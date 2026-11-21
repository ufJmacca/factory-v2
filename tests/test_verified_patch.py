from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from benchmarks.records import fixture_git
from factory import checkpoint, evidence, fixture
from factory.delivery_steps import Delivery
from factory.records_workspace import Candidate
from factory.verify import verify


class PatchCandidate(Candidate):
    def start(self, run_id):
        super().start(run_id)
        repo = self.checkout(run_id)
        (repo / "old-name").write_bytes(b"a file to move\n")
        fixture_git(repo, "add", "old-name")
        fixture_git(repo, "commit", "--amend", "--no-edit")
        return checkpoint.capture(repo, fixture_git(repo, "rev-parse", "HEAD"))

    def improve(self, run_id, red):
        super().improve(run_id, red)
        repo = self.checkout(run_id)
        fixture_git(repo, "mv", "old-name", "new-name")
        (repo / "data.bin").write_bytes(b"\x00\xff\x89\x00")
        (repo / "zz-note").write_bytes(b"trailing spaces  \n")
        fixture_git(repo, "add", "data.bin", "zz-note")
        fixture_git(repo, "commit", "--amend", "--no-edit")
        return checkpoint.capture(repo, red["base_sha"])


class VerifiedPatchTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = evidence.Evidence(root / "archive")
        self.run_id = "patch-run"
        candidate = PatchCandidate(root / "candidate")
        ledger = fixture.Ledger(root / "fixture.sqlite")
        checks = ROOT / "grading" / "checks.json"
        bundle_file = root / "submission.bundle"

        def verify_bundle(operation, *, base, head, bundle, check_digest):
            bundle_file.write_bytes(checkpoint.decode(bundle))
            launch = subprocess.Popen

            def launch_fixture_without_uid_switch(*args, user=None, group=None, extra_groups=None, **kwargs):
                return launch(*args, **kwargs)

            with patch("factory.verify.subprocess.Popen", launch_fixture_without_uid_switch):
                verdict = verify(base=base, head=head, bundle=bundle_file, check_file=checks, check_digest=check_digest)
            return {"verdict": evidence.encode(asdict(verdict)).decode(),
                    "exit_code": int(verdict.decision == "rejected")}

        delivery = Delivery(self.store, checks.read_bytes(), "test-env", root)
        delivery.verifier = verify_bundle
        delivery.candidate = lambda operation, **args: getattr(candidate, operation)(**args)
        delivery.fixture = lambda operation, **args: getattr(ledger, operation)(**args)
        base = candidate.start(self.run_id)["base_sha"]
        request = self.store.admit({
            "schema_version": 1, "run_id": self.run_id, "fixture": "records",
            "base_sha": base, "environment_digest": "test-env",
        })
        prepared = delivery.prepare(request)
        self.manifest = delivery.deliver(request, prepared, {
            "workflow_id": "records-delivery/patch-run", "temporal_run_id": "execution", "delivery_attempt": 1,
        }, lambda: None)
        self.expected = checkpoint.git(
            candidate.checkout(self.run_id), "diff", "--binary", "--no-renames", base, prepared["submitted_sha"],
        )
        candidate.discard(self.run_id)
        bundle_file.unlink()

    def invoke(self, run_id=None):
        return subprocess.run(
            [sys.executable, "-m", "factory.verified_patch", "--evidence", str(self.store.root), run_id or self.run_id],
            cwd=self.store.root.parent,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "GIT_DIR": "/nonexistent"},
            capture_output=True, timeout=30,
        )

    def archive_contents(self):
        return {path.relative_to(self.store.root): path.read_bytes() if path.is_file() else None
                for path in self.store.root.rglob("*")}

    def assert_refused(self, result, diagnostic):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertIn(diagnostic, result.stderr.lower())

    def test_emits_exact_binary_diff_without_using_checkout_or_changing_archive(self):
        before = self.archive_contents()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(b"GIT binary patch\n", self.expected)
        self.assertIn(b"deleted file mode", self.expected)
        self.assertNotIn(b"rename from", self.expected)
        self.assertTrue(self.expected.endswith(b"+trailing spaces  \n"))
        self.assertEqual(result.stdout, self.expected)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(self.archive_contents(), before)

    def test_missing_run_or_bundle(self):
        self.assert_refused(self.invoke("absent"), b"missing")
        self.store.path(self.manifest["artifacts"]["submitted_bundle"]).unlink()
        self.assert_refused(self.invoke(), b"missing")

    def test_corrupt_bundle(self):
        path = self.store.path(self.manifest["artifacts"]["submitted_bundle"])
        path.write_bytes(path.read_bytes() + b"corruption")
        self.assert_refused(self.invoke(), b"corrupt")

    def test_rehashed_verdict_for_a_different_checked_revision(self):
        artifacts = self.manifest["artifacts"]
        verdict = json.loads(self.store.read_blob(artifacts["verifier_verdict"]))
        verdict["checked_sha"] = self.manifest["base_sha"]
        artifacts["verifier_verdict"] = self.store.blob(evidence.encode(verdict))
        (self.store.run_dir(self.run_id) / "manifest.json").write_bytes(evidence.encode(self.manifest))
        self.assert_refused(self.invoke(), b"corrupt")
