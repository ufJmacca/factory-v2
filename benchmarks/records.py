import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def fixture_git(repo: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Factory Fixture",
        GIT_AUTHOR_EMAIL="factory-fixture@example.invalid",
        GIT_AUTHOR_DATE="2000-01-01T00:00:00Z",
        GIT_COMMITTER_NAME="Factory Fixture",
        GIT_COMMITTER_EMAIL="factory-fixture@example.invalid",
        GIT_COMMITTER_DATE="2000-01-01T00:00:00Z",
    )
    return subprocess.check_output(["git", *args], cwd=repo, env=env, text=True).strip()


def init_records(repo: Path):
    fixture_git(repo, "init", "--quiet", "--object-format=sha1", "--template=", "--initial-branch=submission")


def commit_records(repo: Path, revision: str) -> str:
    sources = Path(__file__).with_name("record_sources")
    shutil.copyfile(sources / f"{revision}.py", repo / "records.py")
    fixture_git(repo, "add", "records.py")
    fixture_git(repo, "commit", "--quiet", "-m", f"Records fixture: {revision}")
    return fixture_git(repo, "rev-parse", "HEAD")


def build_fixtures(destination: Path) -> dict[str, str]:
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    heads = {}
    with tempfile.TemporaryDirectory(prefix="factory-records-") as directory:
        repo = Path(directory)
        init_records(repo)
        for revision in ("base", "correct", "mutant"):
            if revision == "mutant":
                fixture_git(repo, "reset", "--hard", "--quiet", heads["base"])
            heads[revision] = commit_records(repo, revision)
            bundle = destination / f"{revision}.bundle"
            fixture_git(repo, "bundle", "create", str(bundle), "refs/heads/submission")
    return heads


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the three records submissions.")
    parser.add_argument("destination", type=Path)
    for name, head in build_fixtures(parser.parse_args().destination).items():
        print(f"{name}\t{head}")
