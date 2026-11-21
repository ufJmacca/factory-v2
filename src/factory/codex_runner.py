import json
import os
from pathlib import Path
import re
from selectors import DefaultSelector, EVENT_READ
import signal
import subprocess
from tempfile import TemporaryFile
from time import time

from factory.evidence import encode


MAX_EVENT_BYTES = 1024 * 1024
MAX_STREAM_BYTES = 32 * MAX_EVENT_BYTES
SECRET = re.compile(
    r"(?i)\b(?:sk-[a-z0-9_-]{8,}|gh[pousr]_[a-z0-9_]{8,}|"
    r"github_pat_[a-z0-9_]{8,}|eyJ[a-z0-9_-]+\.[a-z0-9_-]+\.[a-z0-9_-]+|"
    r"Bearer\s+[a-z0-9._=-]{8,}|(?:access|refresh|id|api)[_-](?:token|key)"
    r"[\s\"':=]+[a-z0-9._=-]{8,})"
)


def redact(value):
    if isinstance(value, str):
        return SECRET.sub("[redacted]", value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "[redacted]" if re.search(r"(?i)credential|authorization|api_key|access_token|refresh_token", key)
            else redact(item)
            for key, item in value.items()
        }
    return value


def subscription_failure(message: str) -> str | None:
    message = message.casefold()
    if any(term in message for term in (
        "usage limit", "quota exceeded", "insufficient_quota", "usage_limit_reached",
        "usage quota", "rate limit exceeded",
    )):
        return "quota_exhausted"
    if any(term in message for term in (
        "access token could not be refreshed", "sign in again", "not logged in",
        "token_expired", "token_revoked", "401 unauthorized", "authentication required",
    )):
        return "login_required"
    return None


def run_cli(argv: list[str], cwd: Path, env: dict, prompt: bytes, events: Path, deadline: float,
            *, cancelled=lambda: False) -> dict:
    reason = None
    failure = None
    usage = None
    lines = 0
    size = 0
    with events.open("xb") as archive, TemporaryFile() as stdin, DefaultSelector() as selector:
        stdin.write(prompt)
        stdin.seek(0)
        read_out, write_out = os.pipe()
        read_err, write_err = os.pipe()
        try:
            selector.register(read_out, EVENT_READ, "stdout")
            selector.register(read_err, EVENT_READ, "stderr")
            try:
                if cancelled():
                    return {"reason": "lease_lost", "exit_code": None, "event_count": 0,
                            "usage": None, "billing": None}
                if time() >= deadline:
                    return {"reason": "deadline_exceeded", "exit_code": None, "event_count": 0,
                            "usage": None, "billing": None}
                process = subprocess.Popen(
                    argv, cwd=cwd, env=env, stdin=stdin, stdout=write_out, stderr=write_err,
                    start_new_session=True,
                )
            finally:
                os.close(write_out)
                os.close(write_err)
            buffers = {read_out: b"", read_err: b""}
            try:
                while selector.get_map() or process.poll() is None:
                    if cancelled():
                        reason = "lease_lost"
                        break
                    remaining = deadline - time()
                    if remaining <= 0:
                        reason = "deadline_exceeded"
                        break
                    for stream, _ in selector.select(min(remaining, 0.25)):
                        fd = stream.fd
                        chunk = os.read(fd, 65536)
                        data = buffers[fd] + chunk
                        if chunk:
                            records = data.split(b"\n")
                            buffers[fd] = records.pop()
                        else:
                            selector.unregister(fd)
                            records = [data] if data else []
                        size += len(chunk)
                        if size > MAX_STREAM_BYTES or any(len(line) > MAX_EVENT_BYTES for line in records) \
                                or len(buffers[fd]) > MAX_EVENT_BYTES:
                            reason = "event_limit_exceeded"
                            break
                        for line in records:
                            if stream.data == "stderr":
                                failure = subscription_failure(line.decode(errors="replace")) or failure
                                continue
                            try:
                                event = json.loads(line)
                                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                                    raise ValueError("invalid event")
                            except (ValueError, RecursionError):
                                event = {"type": "collector.invalid_event"}
                            if event["type"] in ("error", "turn.failed"):
                                failure = subscription_failure(json.dumps(event)) or failure
                            if event["type"] == "turn.completed":
                                usage = event.get("usage")
                            archive.write(encode(redact(event)))
                            lines += 1
                        archive.flush()
                        os.fsync(archive.fileno())
                    if reason:
                        break
                if reason is None:
                    process.wait(timeout=max(0, deadline - time()))
            except subprocess.TimeoutExpired:
                reason = "deadline_exceeded"
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        finally:
            os.close(read_out)
            os.close(read_err)
    if reason is None:
        if process.returncode < 0:
            reason = "interrupted"
        elif process.returncode == 0:
            reason = "completed"
        else:
            reason = failure or "cli_failed"
    return {"reason": reason, "exit_code": process.returncode, "event_count": lines,
            "usage": redact(usage), "billing": None}
