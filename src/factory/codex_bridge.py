import json
from pathlib import Path
import subprocess
import sys

from factory.codex_runner import redact
from factory.codex_workspace import Workspace
from factory.evidence import Conflict, Evidence, encode
from factory.harbor_binding import Binding, LeaseLost


PHASES = ("investigation", "red", "green", "refactor", "submission")
TOOLS = [
    {
        "name": "workspace",
        "description": "Run a shell command in the assigned offline product checkout. Use this for all file and Git operations.",
        "inputSchema": {
            "type": "object", "required": ["command"], "additionalProperties": False,
            "properties": {"command": {"type": "string"}},
        },
    },
    {
        "name": "milestone",
        "description": "Record a phase after doing the work. For red, commit only the new behavioral tests and provide their argv. For green and submission, the runner reruns that command. Commit all changes before red, green and submission.",
        "inputSchema": {
            "type": "object", "required": ["phase", "note"], "additionalProperties": False,
            "properties": {
                "phase": {"enum": list(PHASES)}, "note": {"type": "string"},
                "argv": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
]

MARKER_SCRIPT = """
import os, stat, sys

directory = os.open('.git', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
if len(sys.argv) == 2:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open('factory-marker', flags, 0o600, dir_fd=directory), 'wb') as marker:
        marker.write(sys.argv[1].encode())
else:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open('factory-marker', flags, dir_fd=directory), 'rb') as marker:
        if not stat.S_ISREG(os.fstat(marker.fileno()).st_mode):
            raise ValueError('workspace marker is not a regular file')
        sys.stdout.buffer.write(marker.read(128))
"""
MARKER_ARGV = ["/opt/venv/bin/python", "-IB", "-c", MARKER_SCRIPT]


class WorkspaceProof:
    def __init__(self, store: Evidence, binding: Binding, attempt: int):
        self.store = store
        self.binding = binding
        self.attempt = attempt
        self.run_id = binding.name
        self.key = f"attempt-{attempt}-workspace-proof"
        self.marker = f"{self.run_id}:{attempt}"

    def command(self, argv):
        with self.binding.records.lock(self.run_id):
            if self.store.read(self.run_id, f"{self.key}-rejected") is not None:
                raise Conflict("workspace marker proof was rejected")
            try:
                if self.store.read(self.run_id, self.key) is None:
                    return self.establish(argv)
                return self.binding.run_scoped(argv, self.attempt, "workspace-command")["result"]
            except LeaseLost:
                raise
            except Conflict:
                self.store.record(self.run_id, f"{self.key}-rejected", {"rejected": True})
                raise

    def establish(self, argv):
        # The first call and both marker operations have stable IDs so a lost reply can be read again.
        call_id = f"proof-{self.attempt}-command"
        pending = self.binding.records.read(self.run_id, f"call-{call_id}")
        first_argv = pending["argv"] if pending is not None else argv
        command = self.binding.run_scoped(first_argv, self.attempt, "workspace-command", call_id=call_id)
        written = self.binding.run_scoped([*MARKER_ARGV, self.marker], self.attempt, "marker-write",
                                          call_id=f"proof-{self.attempt}-write")
        if written["result"]["exit_code"] or written["result"]["truncated"]:
            raise Conflict("workspace marker could not be created exclusively")
        read = self.binding.run_scoped(MARKER_ARGV, self.attempt, "marker-read", call_id=f"proof-{self.attempt}-read")
        if not self.matches(read["result"]):
            raise Conflict("workspace marker readback differs")
        self.store.record(self.run_id, self.key, {
            "rollout": self.binding.rollout, "attempt": self.attempt, "marker": self.marker,
            "command_receipt": command["receipt"], "write_receipt": written["receipt"], "read_receipt": read["receipt"],
        })
        if first_argv != argv:
            return self.binding.run_scoped(argv, self.attempt, "workspace-command")["result"]
        return command["result"]

    def matches(self, result):
        return not result["exit_code"] and not result["truncated"] and result["output"] == self.marker

    def postflight(self):
        record = {"assigned_workspace": False, "marker": self.marker}
        proof = self.store.read(self.run_id, self.key)
        if (proof is None or self.store.read(self.run_id, f"{self.key}-rejected") is not None
                or (proof.get("rollout"), proof.get("attempt"), proof.get("marker"))
                != (self.binding.rollout, self.attempt, self.marker)):
            return record
        try:
            for key, operation in (("command", "workspace-command"), ("write", "marker-write"), ("read", "marker-read")):
                self.binding.check_receipt(proof.get(f"{key}_receipt"), self.attempt, operation, f"proof-{self.attempt}-{key}")
            reply = self.binding.run_scoped(MARKER_ARGV, self.attempt, "marker-postflight")
        except LeaseLost:
            raise
        except Conflict:
            return record
        return {**record, "assigned_workspace": self.matches(reply["result"]),
                "workspace_proof": self.store.blob(encode(proof)), "postflight_receipt": reply["receipt"]}


class Bridge:
    def __init__(self, store: Evidence, run_id: str, workspace: Workspace | Binding, base: str,
                 *, proof_attempt: int | None = None):
        if proof_attempt is not None and (not isinstance(workspace, Binding) or workspace.name != run_id
                                          or workspace.root != store.root or proof_attempt < 1):
            raise Conflict("workspace proof requires the admitted Harbor binding and attempt")
        self.store = store
        self.run_id = run_id
        self.workspace = workspace
        self.base = base
        self.workspace_proof = WorkspaceProof(store, workspace, proof_attempt) if proof_attempt is not None else None

    def milestone(self, phase: str, note: str, argv: list[str] | None = None) -> dict:
        if phase not in PHASES or not isinstance(note, str) or not 0 < len(note.strip()) <= 4000:
            raise Conflict("invalid phase or note")
        require_phase = next(
            (name for name in PHASES if self.store.read(self.run_id, name) is None), None,
        )
        if phase != require_phase:
            raise Conflict(f"next phase is {require_phase}")
        if phase != "red" and argv is not None:
            raise Conflict("choose the behavioral check at red")
        record = {"note": redact(note)}
        if phase in ("red", "green", "submission"):
            head = self.git("rev-parse", "HEAD").strip()
            if self.git("status", "--porcelain"):
                raise Conflict("commit the files for this phase before recording it")
            if phase == "red":
                if (not isinstance(argv, list) or not 0 < len(argv) <= 30
                        or not all(isinstance(arg, str) and 0 < len(arg) <= 300 for arg in argv)
                        or argv[0] not in ("python", "python3", "uv")):
                    raise Conflict("provide the behavioral check as python or uv argv")
                changed = self.git("diff", "--name-only", self.base, head).splitlines()
                if (head == self.base or self.git("rev-parse", "HEAD^").strip() != self.base
                        or not changed or any(not path.startswith("tests/") for path in changed)):
                    raise Conflict("red commit must add the tests to the base with no product edit")
            else:
                red = self.store.read(self.run_id, "red")
                argv = red["argv"]
                if phase == "green" and head == red["head_sha"]:
                    raise Conflict("green requires a product commit")
                self.git("merge-base", "--is-ancestor", red["head_sha"], head)
                if self.git("diff", "--name-only", red["head_sha"], head, "--", "tests/"):
                    raise Conflict("the behavioral tests changed since red")
            result = self.workspace.run(argv)
            if result["truncated"] or result["exit_code"] != int(phase == "red"):
                raise Conflict(f"{phase} check exited {result['exit_code']}: {redact(result['output'])[:2000]}")
            if self.git("status", "--porcelain") or self.git("rev-parse", "HEAD").strip() != head:
                raise Conflict("behavioral check changed the submission")
            snapshot = self.workspace.snapshot(self.base)
            if snapshot["head_sha"] != head:
                raise Conflict("checkpoint head changed")
            record.update(
                head_sha=head, argv=argv, test=self.store.blob(encode(redact(result))),
                checkpoint=self.store.blob(encode({"schema_version": 1, "last_completed_step": phase, **snapshot})),
            )
        return self.store.record(self.run_id, phase, record)

    def git(self, *args):
        result = self.workspace.run(["git", *args])
        if result["exit_code"] or result["truncated"]:
            raise Conflict("workspace Git check failed")
        return result["output"]

    def call(self, name: str, arguments: dict):
        if name == "workspace":
            command = arguments.get("command") if isinstance(arguments, dict) else None
            if not isinstance(command, str) or arguments.keys() != {"command"} or not 0 < len(command) <= 12_000:
                raise Conflict("invalid workspace command")
            if self.store.read(self.run_id, "submission") is not None:
                raise Conflict("workspace is closed after submission")
            argv = ["/bin/sh", "-c", command]
            result = self.workspace_proof.command(argv) if self.workspace_proof else self.workspace.run(argv)
            return redact(result)
        if name == "milestone":
            return self.milestone(**arguments)
        raise Conflict("tool is outside the delivery profile")


def serve(bridge: Bridge):
    for line in sys.stdin.buffer:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request["method"]
        result = {
            "initialize": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                           "serverInfo": {"name": "factory-workspace", "version": "1"}},
            "tools/list": {"tools": TOOLS},
            "ping": {},
        }.get(method)
        reply = {"jsonrpc": "2.0", "id": request["id"]}
        if method == "tools/call":
            params = request["params"]
            try:
                content = bridge.call(params["name"], params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(content)}]}
            except (TypeError, ValueError, OSError, subprocess.SubprocessError) as error:
                message = str(error) if isinstance(error, Conflict) else "workspace request failed"
                result = {"content": [{"type": "text", "text": message}], "isError": True}
        if result is None:
            reply["error"] = {"code": -32601, "message": "method unavailable"}
        else:
            reply["result"] = result
        sys.stdout.buffer.write(encode(reply))
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    evidence_root, run_id, workspace, base, *rollout = sys.argv[1:]
    root = Path(evidence_root)
    bound = Binding(root, rollout[0]) if rollout else Workspace(workspace)
    if bound.name != workspace or (rollout and bound.name != run_id):
        raise Conflict("workspace binding does not match the run")
    proof_attempt = int(rollout[1]) if len(rollout) == 2 else None
    serve(Bridge(Evidence(root), run_id, bound, base, proof_attempt=proof_attempt))
