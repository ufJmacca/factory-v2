from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory import checkpoint, evidence, fixture
from factory.records_workspace import Candidate


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_restores_staged_working_and_untracked_files_after_checkout_loss(self):
        candidate = Candidate(self.root / "original")
        red = candidate.start("delivery")
        repo = candidate.root / "delivery"
        (repo / "notes").write_text("staged\n")
        checkpoint.git(repo, "add", "notes")
        (repo / "notes").write_text("working\n")
        (repo / "required.bin").write_bytes(b"\x00required\xff")
        (repo / "required.bin").chmod(0o755)
        snapshot = checkpoint.capture(repo, red["base_sha"])
        candidate.discard("delivery")

        recovered = Candidate(self.root / "new")
        self.assertEqual(recovered.restore("delivery", snapshot), snapshot["tree"])
        restored = recovered.root / "delivery"
        self.assertEqual(checkpoint.git(restored, "show", ":notes"), b"staged\n")
        self.assertEqual((restored / "notes").read_text(), "working\n")
        self.assertEqual((restored / "required.bin").read_bytes(), b"\x00required\xff")
        self.assertTrue((restored / "required.bin").stat().st_mode & 0o100)
        self.assertEqual(checkpoint.git(restored, "rev-parse", "HEAD").strip().decode(), red["base_sha"])

    def test_tampered_checkpoint_cannot_restore(self):
        candidate = Candidate(self.root / "candidate")
        snapshot = candidate.start("delivery")
        snapshot["tree"] = "0" * 40
        with self.assertRaises(checkpoint.InvalidCheckpoint):
            candidate.restore("delivery", snapshot)
        self.assertFalse((candidate.root / "delivery").exists())

    def test_falls_back_to_verified_green_or_reports_missing(self):
        store = evidence.Evidence(self.root)
        green = store.blob(b"verified green")
        submission = store.blob(b"verified submission")
        store.path(submission).unlink()
        self.assertEqual(store.latest_checkpoint(submission, green), ("green", b"verified green"))
        store.path(green).unlink()
        self.assertIsNone(store.latest_checkpoint(submission, green))

    def test_conflicting_checkpoint_does_not_fall_back(self):
        store = evidence.Evidence(self.root)
        green = store.blob(b"verified green")
        submission = store.blob(b"verified submission")
        store.path(submission).write_bytes(b"corrupt")
        with self.assertRaises(evidence.Conflict):
            store.latest_checkpoint(submission, green)

    def test_duplicate_receipts_survive_reopen_and_conflicts_fail_closed(self):
        database = self.root / "fixture.sqlite"
        ledger = fixture.Ledger(database)
        first = ledger.apply("records-delivery:delivery", "a" * 64)
        reopened = fixture.Ledger(database)
        self.assertEqual(reopened.lookup("records-delivery:delivery"), first)
        self.assertEqual(reopened.apply("records-delivery:delivery", "a" * 64), first)
        with self.assertRaises(evidence.Conflict):
            reopened.apply("records-delivery:delivery", "b" * 64)
        fixture.validate_receipt(first, "records-delivery:delivery", "a" * 64)
        with self.assertRaises(evidence.Conflict):
            fixture.validate_receipt(first, "records-delivery:delivery", "b" * 64)
        self.assertEqual(reopened.inspect()["effect_count"], 1)

    def test_identical_run_request_keeps_its_identity(self):
        store = evidence.Evidence(self.root)
        request = {"schema_version": 1, "run_id": "delivery", "fixture": "records"}
        store.admit(request)
        self.assertEqual(evidence.Evidence(self.root).admit(dict(request)), request)
        with self.assertRaises(evidence.Conflict):
            store.admit({**request, "fixture": "different"})


if __name__ == "__main__":
    unittest.main()
