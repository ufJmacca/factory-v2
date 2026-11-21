from contextlib import contextmanager
import json
import re
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory, TemporaryFile

from factory import checkpoint
from factory.evidence import encode


class WorkspaceError(ValueError):
    pass


def docker(*args, input=None, timeout=60):
    return subprocess.run(["docker", *args], input=input, capture_output=True, check=True, timeout=timeout).stdout


def seed(repo: Path, base: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise WorkspaceError("base must be a full Git SHA")
    with TemporaryDirectory() as directory:
        checkout = Path(directory)
        checkpoint.git(checkout, "init")
        checkpoint.git(checkout, "fetch", "--no-tags", "--", str(repo.resolve()), base)
        checkpoint.git(checkout, "reset", "--hard", "FETCH_HEAD")
        return checkpoint.capture(checkout, base)


class Workspace:
    ROOT = "/work/product"

    def __init__(self, name: str):
        self.name = name

    @contextmanager
    def provision(self, image: str, snapshot: dict):
        docker(
            "run", "-d", "--pull=never", "--name", self.name, "--init", "--network", "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--pids-limit", "80", "--memory", "768m", "--cpus", "2",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
            "--tmpfs", "/work:rw,nosuid,nodev,size=256m,mode=0755",
            "--env", "HOME=/tmp", "--env", "GIT_CONFIG_NOSYSTEM=1",
            "--env", "GIT_CONFIG_GLOBAL=/dev/null", image, "sleep", "infinity",
        )
        try:
            self.internal("restore", input=encode(snapshot))
            yield self
        finally:
            docker("rm", "-f", self.name, timeout=15)

    def internal(self, operation: str, *args: str, input=None) -> bytes:
        code = {
            "restore": "print(checkpoint.restore(repo, json.load(sys.stdin)))",
            "capture": "print(json.dumps(checkpoint.capture(repo, sys.argv[1])))",
        }[operation]
        source = (
            "import json, sys; from pathlib import Path; sys.path.insert(0, '/factory/src'); "
            f"from factory import checkpoint; repo = Path({self.ROOT!r}); {code}"
        )
        return docker("exec", "-i", "-w", "/", self.name, "python", "-IB", "-c", source, *args, input=input)

    def snapshot(self, base: str) -> dict:
        return json.loads(self.internal("capture", base))

    def run(self, argv: list[str], *, limit: int = 30_000) -> dict:
        with TemporaryFile() as output:
            result = subprocess.run(
                ["docker", "exec", "-w", self.ROOT, self.name, "timeout", "-k", "1s", "45s", *argv],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                timeout=55,
            )
            output.seek(0)
            data = output.read(limit + 1)
        return {
            "exit_code": result.returncode, "output": data[:limit].decode(errors="replace"),
            "truncated": len(data) > limit,
        }
