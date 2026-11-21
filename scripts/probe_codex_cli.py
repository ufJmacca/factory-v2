"""Drive the installed Codex CLI against a local scripted Responses endpoint."""
from contextlib import contextmanager
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shlex
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from time import time

from factory.codex_delivery import cli_args, cli_environment
from factory.codex_runner import run_cli
from factory.codex_transport import KEY_NAME
from factory.codex_workspace import Workspace, seed
from factory.evidence import Evidence


BASE = "31f67b2ea40d4fa4570b26dd9b175e9fe423f416"
MODEL = "gpt-6.2-sol"
RUN_ID = "fixture-cli"
TEST = """\
from pathlib import Path
import unittest

class Fixture(unittest.TestCase):
    def test_marker(self):
        self.assertEqual(Path("factory-marker.txt").read_bytes(), b"factory-ready")
"""
COMMIT = "git -c user.name=Fixture -c user.email=fixture@example.invalid commit"
COMMAND = ["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_codex_fixture.py"]
CALLS = [
    ("workspace", {"command": "git rev-parse --show-toplevel"}),
    ("milestone", {"phase": "investigation", "note": "Verified checkout placement."}),
    ("workspace", {"command": f"printf %s {shlex.quote(TEST)} > tests/test_codex_fixture.py && "
                             f"git add tests/test_codex_fixture.py && {COMMIT} -m 'Add behavior test'"}),
    ("milestone", {"phase": "red", "note": "The required marker is missing on the base.", "argv": COMMAND}),
    ("workspace", {"command": "printf factory-ready > factory-marker.txt && "
                             f"git add factory-marker.txt && {COMMIT} -m 'Write marker'"}),
    ("milestone", {"phase": "green", "note": "The same check now reads the marker."}),
    ("milestone", {"phase": "refactor", "note": "The one-file implementation needs no cleanup."}),
    ("milestone", {"phase": "submission", "note": "The committed revision is ready for independent checks."}),
]


class Responses(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        if self.path != "/v1/responses" or self.headers.get("Authorization") != "Bearer test-key":
            self.send_error(401)
            return
        payload = self.rfile.read(int(self.headers["Content-Length"]))
        if self.headers.get("Content-Encoding") == "gzip":
            payload = gzip.decompress(payload)
        request = json.loads(payload)
        turn = self.server.turn
        self.server.turn += 1
        offered = {tool.get("name") for tool in request["tools"]}
        if offered & {"shell", "shell_command", "exec_command"}:
            self.send_error(400, "native shell was exposed")
            return
        if turn < len(CALLS):
            name, arguments = CALLS[turn]
            namespace = next((tool for tool in request["tools"]
                              if tool.get("name") == "mcp__factory" and tool.get("type") == "namespace"), {})
            members = {tool["name"] for tool in namespace.get("tools", [])}
            if name not in members:
                self.send_error(400, "factory workspace tool unavailable")
                return
            item = {"type": "function_call", "name": name, "namespace": "mcp__factory",
                    "arguments": json.dumps(arguments), "id": f"fc-{turn}",
                    "call_id": f"call-{turn}", "status": "completed"}
        else:
            item = {"type": "message", "role": "assistant", "id": "message", "status": "completed",
                    "content": [{"type": "output_text", "text": "Finished fixture.", "annotations": []}]}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def event(kind, **fields):
            self.wfile.write(f"event: {kind}\ndata: ".encode() + json.dumps({"type": kind, **fields}).encode()
                             + b"\n\n")

        response = {"id": f"resp-{turn}", "object": "response", "created_at": int(time()), "model": MODEL}
        event("response.created", response={**response, "status": "in_progress", "output": []})
        initial = {**item, "status": "in_progress"}
        initial["arguments" if turn < len(CALLS) else "content"] = "" if turn < len(CALLS) else []
        event("response.output_item.added", output_index=0, item=initial)
        event("response.output_item.done", output_index=0, item=item)
        event("response.completed", response={
            **response, "status": "completed", "output": [item],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        })


@contextmanager
def endpoint():
    with ThreadingHTTPServer(("127.0.0.1", 0), Responses) as server:
        server.turn = 0
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1"
        finally:
            server.shutdown()
            thread.join()


def main(image, name, root, cli):
    archive = Path(root)
    store = Evidence(archive / "cli-archive")
    workspace = Workspace(f"{name}-cli")
    initial = seed(Path.cwd(), BASE)
    with endpoint() as url, TemporaryDirectory(prefix="factory-fixture-cli-") as temporary:
        directory = Path(temporary).resolve()
        cwd, home = directory / "workspace", directory / "codex"
        cwd.mkdir()
        home.mkdir()
        env = {**cli_environment(home), KEY_NAME: "test-key"}
        request = {"run_id": RUN_ID, "base_sha": BASE, "transport": {"kind": "cli-proxy", "url": url}}
        argv = cli_args(cli, cwd, store, request, workspace, {"model": MODEL, "reasoning_effort": "medium"})
        path = archive / "scripted-cli.jsonl"
        with workspace.provision(image, initial):
            result = run_cli(argv, cwd, env, b"Run the scripted fixture.", path, time() + 60)
        events = [json.loads(line) for line in path.read_text().splitlines()]
        failures = [
            event["item"]["error"] for event in events if event.get("item", {}).get("type") == "mcp_tool_call"
            and event["item"].get("error")
        ]
        if result["reason"] != "completed" or failures:
            raise SystemExit(f"scripted CLI tool execution failed: {failures or result}")
    submission = store.read(RUN_ID, "submission")
    if submission is None:
        raise SystemExit("scripted CLI produced no submission")
    final = json.loads(store.read_blob(submission["checkpoint"]))
    with workspace.provision(image, final):
        if workspace.run(["git", "rev-parse", "HEAD"])["output"].strip() != submission["head_sha"]:
            raise SystemExit("reconstructed the wrong submission")
        if workspace.run(COMMAND)["exit_code"]:
            raise SystemExit("submitted fixture check failed on independent checkout")
    print("Installed Codex CLI executed all scripted tools and restored its submitted SHA in a fresh container.")


if __name__ == "__main__":
    main(*sys.argv[1:])
