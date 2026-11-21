from base64 import b64decode, b64encode
from pathlib import Path
import shutil
import stat
import subprocess
from tempfile import TemporaryDirectory

from factory.verify import GIT_ENV, SUBMISSION_REF, Rejected, reconstruct


class InvalidCheckpoint(ValueError):
    pass


def git(repo: Path, *args: str, input: bytes | None = None, env: dict | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args], input=input, cwd=repo, env={**GIT_ENV, **(env or {})},
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
    )
    return result.stdout


def encode(data: bytes) -> str:
    return b64encode(data).decode("ascii")


def decode(data: str) -> bytes:
    return b64decode(data, validate=True)


def tree(repo: Path) -> str:
    with TemporaryDirectory() as directory:
        env = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
        git(repo, "read-tree", "HEAD", env=env)
        git(repo, "add", "--all", env=env)
        return git(repo, "write-tree", env=env).strip().decode()


def working_state(repo: Path) -> dict:
    paths = git(repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
    untracked = {}
    for encoded in paths[:-1]:
        name = encoded.decode()
        path = repo / name
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise InvalidCheckpoint(f"untracked file is not regular: {name}")
        untracked[name] = {"bytes": encode(path.read_bytes()), "executable": bool(mode & 0o100)}
    diff = ("diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames")
    return {
        "index_patch": encode(git(repo, *diff, "--cached", "HEAD")),
        "working_patch": encode(git(repo, *diff)),
        "untracked": untracked,
        "tree": tree(repo),
    }


def capture(repo: Path, base_sha: str) -> dict:
    head = git(repo, "rev-parse", "HEAD").strip().decode()
    git(repo, "update-ref", SUBMISSION_REF, head)
    with TemporaryDirectory() as directory:
        bundle = Path(directory) / "submission.bundle"
        git(repo, "bundle", "create", str(bundle), SUBMISSION_REF)
        return {
            "base_sha": base_sha, "head_sha": head, "bundle": encode(bundle.read_bytes()),
            **working_state(repo),
        }


def restore(repo: Path, snapshot: dict) -> str:
    repo.mkdir(parents=True)
    try:
        with TemporaryDirectory() as directory:
            bundle = Path(directory) / "submission.bundle"
            bundle.write_bytes(decode(snapshot["bundle"]))
            reconstruct(repo, bundle, snapshot["base_sha"], snapshot["head_sha"])
        for name, args in (("index_patch", ("--index",)), ("working_patch", ())):
            if snapshot[name]:
                git(repo, "apply", *args, "-", input=decode(snapshot[name]))
        for name, file in snapshot["untracked"].items():
            path = repo / name
            if path.resolve() != path or not path.is_relative_to(repo) or ".git" in path.relative_to(repo).parts:
                raise InvalidCheckpoint("unsafe untracked file path")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(decode(file["bytes"]))
            path.chmod(0o755 if file["executable"] else 0o644)
        restored = working_state(repo)
        if any(snapshot[name] != value for name, value in restored.items()):
            raise InvalidCheckpoint("restored working state differs from checkpoint")
    except (OSError, ValueError, KeyError, Rejected, subprocess.SubprocessError) as error:
        shutil.rmtree(repo)
        raise InvalidCheckpoint(f"checkpoint restore failed: {error}") from error
    return restored["tree"]
