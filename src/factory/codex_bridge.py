import json
from pathlib import Path
import subprocess
import sys

from factory.codex_runner import redact
from factory.codex_workspace import Workspace
from factory.evidence import Conflict, Evidence, encode


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


class Bridge:
    def __init__(self, store: Evidence, run_id: str, workspace: Workspace, base: str):
        self.store = store
        self.run_id = run_id
        self.workspace = workspace
        self.base = base

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
            command = arguments.get("command")
            if arguments.keys() != {"command"} or not isinstance(command, str) or not 0 < len(command) <= 12_000:
                raise Conflict("invalid workspace command")
            if self.store.read(self.run_id, "submission") is not None:
                raise Conflict("workspace is closed after submission")
            return redact(self.workspace.run(["/bin/sh", "-c", command]))
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
    evidence_root, run_id, workspace, base = sys.argv[1:]
    serve(Bridge(Evidence(Path(evidence_root)), run_id, Workspace(workspace), base))
