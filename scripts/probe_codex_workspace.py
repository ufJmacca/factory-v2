"""Check the actual candidate container and the tool path without making model calls."""
import json
from pathlib import Path
import shlex
import sys

from factory.codex_bridge import Bridge
from factory.codex_workspace import Workspace, docker, seed
from factory.evidence import Evidence


def main(image, name, root):
    output = Path(root)
    sentinel = output / "host-only"
    sentinel.write_text("factory-sentinel")
    base = "31f67b2ea40d4fa4570b26dd9b175e9fe423f416"
    snapshot = seed(Path.cwd(), base)
    workspace = Workspace(name)
    with workspace.provision(image, snapshot):
        [container] = json.loads(docker("inspect", workspace.name))
        host = container["HostConfig"]
        if container["Mounts"] or host["NetworkMode"] != "none" or not host["ReadonlyRootfs"]:
            raise SystemExit("candidate mount, network or root filesystem policy differs")
        bridge = Bridge(Evidence(output / "archive"), "probe", workspace, base)
        result = bridge.call("workspace", {"command": (
            "set -eu\n"
            'test "$PWD" = /work/product\n'
            f'test "$(git rev-parse HEAD)" = {base}\n'
            f'test ! -e {shlex.quote(str(sentinel))}\n'
            'test ! -e /var/run/docker.sock\n'
            'test ! -e /evidence && test ! -e /grading\n'
            'test -z "${CODEX_HUB_API_KEY-}${OPENAI_API_KEY-}${GH_TOKEN-}"\n'
            'printf "%s" factory-workspace > probe.txt\n'
            'uv run --locked --offline python -c '
            "'from pathlib import Path; print(Path(\"probe.txt\").read_text())'"
        )})
        if result != {"exit_code": 0, "output": "factory-workspace\n", "truncated": False}:
            raise SystemExit(f"candidate tool probe failed: {result}")
    print("The workspace tool restored the pinned base and ran uv in its disposable checkout.")
    print("Docker inspection and the candidate probe confirmed the host sentinel and trusted mounts are absent.")
    print("model_calls=0")


if __name__ == "__main__":
    main(*sys.argv[1:])
