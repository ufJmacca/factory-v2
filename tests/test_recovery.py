from dataclasses import asdict
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory import checkpoint, evidence, fixture
from factory.delivery_steps import Delivery, reopen
from factory.verify import verify
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

    def test_restores_untracked_files_below_a_symlinked_temporary_parent(self):
        candidate = Candidate(self.root / "original")
        initial = candidate.start("delivery")
        repo = candidate.root / "delivery"
        (repo / "untracked.txt").write_text("retained check input")
        snapshot = checkpoint.capture(repo, initial["base_sha"])
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        restored = alias / "restored"
        checkpoint.restore(restored, snapshot)
        self.assertEqual((restored / "untracked.txt").read_text(), "retained check input")

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


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = evidence.Evidence(root / "evidence")
        self.candidate = Candidate(root / "candidate")
        self.ledger = fixture.Ledger(root / "fixture.sqlite")
        self.checks = Path(__file__).resolve().parents[1] / "grading" / "checks.json"
        self.bundle = root / "submission.bundle"
        self.delivery = Delivery(self.store, self.checks.read_bytes(), "test-env", root)
        self.delivery.candidate = lambda operation, **args: getattr(self.candidate, operation)(**args)
        self.delivery.fixture = lambda operation, **args: getattr(self.ledger, operation)(**args)
        self.delivery.verifier = self.verify_bundle
        self.run_id = "delivery"
        base = self.candidate.start(self.run_id)["base_sha"]
        self.request = self.store.admit({
            "schema_version": 1, "run_id": self.run_id, "fixture": "records",
            "base_sha": base, "environment_digest": "test-env",
        })
        self.execution = {
            "workflow_id": "records-delivery/delivery", "temporal_run_id": "execution",
            "delivery_attempt": 2,
        }
        self.prepared = self.delivery.prepare(self.request)
        self.candidate.discard(self.run_id)

    def verify_bundle(self, operation, *, base, head, bundle, check_digest):
        self.bundle.write_bytes(checkpoint.decode(bundle))
        verdict = verify(base=base, head=head, bundle=self.bundle, check_file=self.checks, check_digest=check_digest)
        return {"verdict": evidence.encode(asdict(verdict)).decode(),
                "exit_code": int(verdict.decision == "rejected")}

    def remove_artifact(self, name):
        self.store.path(self.prepared["artifacts"][name]).unlink()

    def deliver(self, after_apply=lambda: None):
        return self.delivery.deliver(self.request, self.prepared, self.execution, after_apply)

    def test_missing_verified_checkpoints_publish_interrupted_manifest_without_effect(self):
        self.remove_artifact("submission_checkpoint")
        self.remove_artifact("green_checkpoint")
        manifest = self.deliver()
        self.assertEqual(manifest, reopen(self.store, self.run_id))
        self.assertEqual(manifest["status"], "interrupted")
        self.assertEqual(manifest["recovery"]["reason"], "missing_verified_checkpoint")
        self.assertEqual(manifest["submitted_sha"], self.prepared["submitted_sha"])
        self.assertEqual(set(manifest["missing_artifacts"]), {"submission_checkpoint", "green_checkpoint"})
        self.assertEqual(self.ledger.inspect()["effect_count"], 0)

    def test_interruption_retains_an_effect_applied_before_checkout_loss(self):
        def lose_worker():
            raise RuntimeError("worker stopped before acknowledgement")

        with self.assertRaises(RuntimeError):
            self.deliver(lose_worker)
        original = self.ledger.lookup("records-delivery:delivery")
        self.assertIsNotNone(original)
        self.assertIsNone(self.store.read(self.run_id, "receipt"))
        self.remove_artifact("submission_checkpoint")
        self.remove_artifact("green_checkpoint")
        manifest = self.deliver()
        self.assertEqual(manifest, reopen(self.store, self.run_id))
        self.assertEqual(manifest["status"], "interrupted")
        self.assertEqual(self.store.read(self.run_id, "receipt"), original)
        self.assertEqual(self.ledger.inspect()["effects"], [{**original, "apply_requests": 1}])

    def test_missing_submission_restores_verified_green_before_applying(self):
        self.remove_artifact("submission_checkpoint")

        def after_apply():
            self.assertEqual(
                (self.candidate.checkout(self.run_id) / "final-record.txt").read_bytes(), b"one\ntwo",
            )
            self.assertIsNotNone(self.store.read(self.run_id, "intent"))

        manifest = self.deliver(after_apply)
        self.assertEqual(manifest, reopen(self.store, self.run_id))
        self.assertEqual(manifest["status"], "delivered")
        self.assertEqual(manifest["recovery"]["status"], "restored")
        self.assertEqual(manifest["recovery"]["checkpoint_step"], "green")
        self.assertEqual(manifest["submitted_sha"], self.prepared["submitted_sha"])
        self.assertEqual(self.ledger.inspect()["effect_count"], 1)

    def test_conflicting_receipt_prevents_delivery(self):
        conflicting = self.ledger.apply("records-delivery:delivery", "e" * 64)
        with self.assertRaises(evidence.Conflict):
            self.deliver()
        self.assertIsNone(self.store.read(self.run_id, "manifest"))
        self.assertIsNone(self.store.read(self.run_id, "receipt"))
        self.assertEqual(self.ledger.inspect()["effects"], [{**conflicting, "apply_requests": 1}])

    def test_missing_verified_bundle_prevents_effect(self):
        self.remove_artifact("submitted_bundle")
        with self.assertRaises(FileNotFoundError):
            self.deliver()
        self.assertEqual(self.ledger.inspect()["effect_count"], 0)


if __name__ == "__main__":
    unittest.main()
