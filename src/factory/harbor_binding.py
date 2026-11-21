import json
from dataclasses import dataclass
from pathlib import Path
from shlex import join, quote
from time import monotonic, sleep, time
from typing import NamedTuple
from uuid import UUID, uuid4

from factory.evidence import Conflict, Evidence, encode


WORKSPACE_CWD = "/work/product"
TRUSTED_WORKSPACE_MARKER = "trusted-round-trip"


class LeaseLost(Conflict):
    pass


class WorkspaceCall(NamedTuple):
    call_id: str
    argv: list[str]
    limit: int
    scope: dict | None


@dataclass(frozen=True)
class Binding:
    root: Path
    rollout: str
    lease_seconds: float = 12

    def __post_init__(self):
        if str(UUID(self.rollout)) != self.rollout:
            raise ValueError("expected the canonical Harbor rollout UUID")

    @property
    def name(self):
        return "eval-" + self.rollout

    @property
    def folder(self):
        return self.root / "bindings" / self.rollout

    @property
    def records(self):
        return Evidence(self.folder)

    def open(self):
        try:
            self.folder.mkdir(parents=True)
        except FileExistsError:
            raise LeaseLost("rollout already has a binding") from None
        self._heartbeat(time())

    def _heartbeat(self, now):
        pending = self.folder / "heartbeat.next"
        pending.write_bytes(encode(now + self.lease_seconds))
        pending.replace(self.folder / "heartbeat")

    def active(self, now=None):
        if self.records.read(self.name, "revoked"):
            return False
        try:
            return (time() if now is None else now) < float((self.folder / "heartbeat").read_text())
        except FileNotFoundError:
            return False

    def pulse(self):
        now = time()
        if not self.active(now):
            raise LeaseLost("Harbor workspace lease is unavailable")
        self._heartbeat(now)

    def require_active(self):
        if not self.active():
            raise LeaseLost("Harbor workspace lease is unavailable")

    def revoke(self, reason):
        self.records.record(self.name, "revoked", {"revoked": True})
        self.records.record(self.name, f"revoked-by-{reason}", {"reason": reason})

    def run(self, argv, *, limit=30_000):
        return self._run(str(uuid4()), {"argv": argv, "limit": limit})

    def run_scoped(self, argv, attempt, operation, *, call_id=None):
        call_id = call_id or str(uuid4())
        scope = {"rollout": self.rollout, "attempt": attempt, "operation": operation}
        reply = self._run(call_id, {"argv": argv, "limit": 30_000, "scope": scope})
        self.check_receipt(reply.get("receipt") if isinstance(reply, dict) else None, attempt, operation, call_id)
        result = reply.get("result")
        if (not isinstance(result, dict) or type(result.get("exit_code")) is not int
                or not isinstance(result.get("output"), str) or type(result.get("truncated")) is not bool):
            raise Conflict("Harbor scoped workspace result is malformed")
        return reply

    def check_receipt(self, receipt, attempt, operation, call_id):
        ready = Evidence(self.root).read(self.name, "harbor-ready") or {}
        if (not ready.get("container") or ready.get("rollout") != self.rollout
                or receipt != {"rollout": self.rollout, "attempt": attempt, "operation": operation,
                               "call_id": call_id, "cwd": WORKSPACE_CWD, "container": ready["container"]}):
            raise Conflict("Harbor workspace receipt identity differs")

    def _run(self, call_id, call):
        self.require_active()
        self.records.record(self.name, f"call-{call_id}", call)
        deadline = monotonic() + 55
        while monotonic() < deadline:
            self.require_active()
            if (result := self.records.read(self.name, f"reply-{call_id}")) is not None:
                self.require_active()
                return result
            sleep(0.04)
        self.revoke("workspace_reply_timeout")
        raise LeaseLost("Harbor workspace reply timed out")

    def snapshot(self, base):
        source = (
            "import json,sys; from pathlib import Path; from factory.checkpoint import capture; "
            "print(json.dumps(capture(Path('/work/product'), sys.argv[1])))"
        )
        result = self.run(["/opt/venv/bin/python", "-IB", "-c",
                           "import sys; sys.path.insert(0, '/factory/src'); " + source, base],
                          limit=16 * 1024 * 1024)
        if result["exit_code"] or result["truncated"]:
            raise Conflict("Harbor checkpoint capture failed")
        return json.loads(result["output"])

    def next_call(self):
        if not self.active():
            return None
        directory = self.records.run_dir(self.name)
        for path in sorted(directory.glob("call-*.json")):
            call_id = path.stem.removeprefix("call-")
            if self.records.read(self.name, f"reply-{call_id}") is None:
                call = self.records.read(self.name, path.stem, required=True)
                return WorkspaceCall(call_id, call["argv"], call["limit"], call.get("scope"))
        return None

    def reply(self, call_id, result):
        self.require_active()
        self.records.record(self.name, f"reply-{call_id}", result)

    async def execute(self, environment, call: WorkspaceCall, container: str):
        if not 0 < call.limit <= 16 * 1024 * 1024:
            raise Conflict("invalid workspace output limit")
        command = join(["timeout", "-k", "1s", "45s", *call.argv])
        drain = f"{{ head -c {call.limit + 1}; cat >/dev/null; }}"
        script = f"{command} 2>&1 | {drain}"
        if call.scope is not None:
            scope = call.scope
            if scope["rollout"] != self.rollout or type(scope["attempt"]) is not int or scope["attempt"] < 1:
                raise Conflict("invalid workspace call scope")
            identity = quote(f"{self.rollout}\t{scope['attempt']}\t{scope['operation']}\t{call.call_id}")
            script = (
                'cwd=$(pwd -P) && container=$(hostname) || exit 125\n'
                f'printf "%s\\t%s\\t%s\\n" {identity} "$cwd" "$container"\n'
                f'test "$cwd" = {quote(WORKSPACE_CWD)} && test "$container" = {quote(container)} || exit 125\n'
                + script
            )
        self.require_active()
        result = await environment.exec(command="bash -o pipefail -c " + quote(script), cwd=WORKSPACE_CWD,
                                        user="10001:10001", timeout_sec=50)
        stdout = result.stdout or ""
        receipt = None
        if call.scope is not None:
            header, _, stdout = stdout.partition("\n")
            fields = header.split("\t")
            if len(fields) == 6 and fields[1] == str(call.scope["attempt"]):
                rollout, attempt, operation, call_id, cwd, observed_container = fields
                receipt = {"rollout": rollout, "attempt": int(attempt), "operation": operation,
                           "call_id": call_id, "cwd": cwd, "container": observed_container}
        data = (stdout + (result.stderr or "")).encode()
        output = {"exit_code": result.return_code, "output": data[:call.limit].decode(errors="replace"),
                  "truncated": len(data) > call.limit}
        return output if call.scope is None else {"receipt": receipt, "result": output}
