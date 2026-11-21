from pathlib import Path
import shutil

from benchmarks.records import commit_records, init_records
from factory import checkpoint
from factory.evidence import validate_run_id


class Candidate:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def checkout(self, run_id: str) -> Path:
        return self.root / validate_run_id(run_id)

    def discard(self, run_id: str):
        repo = self.checkout(run_id)
        if repo.exists():
            shutil.rmtree(repo)

    def start(self, run_id: str) -> dict:
        self.discard(run_id)
        repo = self.checkout(run_id)
        repo.mkdir()
        init_records(repo)
        base = commit_records(repo, "base")
        (repo / "final-record.txt").write_bytes(b"one\ntwo")
        return checkpoint.capture(repo, base)

    def improve(self, run_id: str, red: dict) -> dict:
        self.restore(run_id, red)
        repo = self.checkout(run_id)
        commit_records(repo, "correct")
        return checkpoint.capture(repo, red["base_sha"])

    def restore(self, run_id: str, snapshot: dict) -> str:
        self.discard(run_id)
        return checkpoint.restore(self.checkout(run_id), snapshot)
