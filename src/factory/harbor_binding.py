import json
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep, time
from uuid import UUID, uuid4

from factory.evidence import Conflict, Evidence, encode


class LeaseLost(Conflict):
    pass


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
        self.require_active()
        call_id = str(uuid4())
        self.records.record(self.name, f"call-{call_id}", {"argv": argv, "limit": limit})
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
                return call_id, call["argv"], call["limit"]
        return None

    def reply(self, call_id, result):
        self.require_active()
        self.records.record(self.name, f"reply-{call_id}", result)
