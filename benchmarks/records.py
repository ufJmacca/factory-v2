import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def build_fixtures(destination: Path) -> dict[str, str]:
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    sources = Path(__file__).with_name("record_sources")
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
    heads = {}
    with tempfile.TemporaryDirectory(prefix="factory-records-") as directory:
        repo = Path(directory)

        def git(*args: str) -> str:
            return subprocess.check_output(["git", *args], cwd=repo, env=env, text=True).strip()

        git("init", "--quiet", "--object-format=sha1", "--template=", "--initial-branch=submission")
        for revision in ("base", "correct", "mutant"):
            if revision == "mutant":
                git("reset", "--hard", "--quiet", heads["base"])
            shutil.copyfile(sources / f"{revision}.py", repo / "records.py")
            git("add", "records.py")
            git("commit", "--quiet", "-m", f"Records fixture: {revision}")
            heads[revision] = git("rev-parse", "HEAD")
            bundle = destination / f"{revision}.bundle"
            git("bundle", "create", str(bundle), "refs/heads/submission")
    return heads


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the three records submissions.")
    parser.add_argument("destination", type=Path)
    build_fixtures(parser.parse_args().destination)
