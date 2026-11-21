from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile


class Conflict(ValueError):
    pass


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_run_id(run_id: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", run_id):
        raise ValueError("invalid run ID")
    return run_id


def fsync_directory(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            try:
                os.link(temp, path)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise Conflict(f"conflicting evidence: {path.name}") from None
            fsync_directory(path.parent)
        finally:
            temp.unlink()


class Evidence:
    def __init__(self, root: Path):
        self.root = root

    def path(self, reference: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", reference):
            raise Conflict("invalid artifact digest")
        return self.root / "blobs" / reference

    def blob(self, data: bytes) -> str:
        reference = digest(data)
        publish(self.path(reference), data)
        return reference

    def read_blob(self, reference: str) -> bytes:
        data = self.path(reference).read_bytes()
        if digest(data) != reference:
            raise Conflict(f"corrupt artifact: {reference}")
        return data

    def latest_checkpoint(self, submission: str, green: str) -> tuple[str, bytes] | None:
        for step, reference in (("submission", submission), ("green", green)):
            try:
                return step, self.read_blob(reference)
            except FileNotFoundError:
                continue
        return None

    def run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / validate_run_id(run_id)

    def record(self, run_id: str, name: str, value):
        publish(self.run_dir(run_id) / f"{name}.json", encode(value))
        return value

    def read(self, run_id: str, name: str, *, required: bool = False):
        try:
            return json.loads((self.run_dir(run_id) / f"{name}.json").read_bytes())
        except FileNotFoundError:
            if required:
                raise
            return None

    def admit(self, request: dict) -> dict:
        return self.record(request["run_id"], "request", request)

    @contextmanager
    def lock(self, run_id: str):
        directory = self.run_dir(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / ".lock").open("ab") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
