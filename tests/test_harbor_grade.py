from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory.evidence import Conflict, Evidence, digest
from factory.harbor_receipt import apply_fixture, reconcile_grade


class HarborGradeTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Evidence(Path(directory.name))
        self.bundle = b"retained submission bundle"
        self.spec = {"job": "job-1", "rollout": "rollout-1", "task_checksum": "locked-task",
                     "harbor_task_lock": "sha256:locked-task"}
        self.request = {"run_id": "eval-test", "evaluation": self.spec, "grader_digest": "frozen-grader"}
        self.store.record("eval-test", "submission", {"head_sha": "submission"})
        self.store.record("eval-test", "provider-stop", {"container": "abc123000000", "removed": True})
        self.export = {
            "submitted_sha": "submission", "grader_digest": "frozen-grader",
            "artifacts": {"submission.bundle": {"sha256": digest(self.bundle)}},
        }
        self.rollout = {
            "id": "rollout-1", "job_id": "job-1", "state": "finished",
            "lock": {"task": {"digest": "sha256:locked-task"}},
            "result": {"id": "rollout-1", "task_checksum": "locked-task", "verifier_environment_mode": "separate",
                       "verifier_result": {"rewards": {"reward": 1}}},
        }
        self.grade = {
            "submitted_sha": "submission", "bundle_sha256": digest(self.bundle), "status": "accepted",
            "reason": "all_checks_passed",
            "checks": dict.fromkeys(("verified", "unknown_run", "missing_artifact", "corrupt"), True),
            "isolation": {**dict.fromkeys(("root_read_only", "tests_read_only", "agent_workspace_absent",
                                         "agent_marker_absent", "docker_socket_absent", "loopback_only"), True),
                          "container": "def456000000"},
        }

    def reconcile(self, bundle=None):
        return reconcile_grade(self.store, self.request, self.export, self.rollout,
                               self.grade, self.bundle if bundle is None else bundle)

    def test_rejects_missing_verifier_isolation_evidence(self):
        self.grade["isolation"].clear()
        with self.assertRaises(Conflict):
            self.reconcile()
        self.assertIsNone(self.store.read("eval-test", "verify"))

    def test_rejects_different_harbor_bundle(self):
        with self.assertRaises(Conflict):
            self.reconcile(b"different")
        self.assertIsNone(self.store.read("eval-test", "verify"))

    def test_persists_one_grade_and_one_fixture_effect_on_replay(self):
        first = self.reconcile()
        self.assertEqual(first, self.reconcile())
        self.assertEqual(first["reason"], "accepted")
        receipt = apply_fixture(self.store, self.request, first)
        self.assertEqual(receipt, apply_fixture(self.store, self.request, first))


if __name__ == "__main__":
    unittest.main()
