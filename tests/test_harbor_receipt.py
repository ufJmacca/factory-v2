import unittest

from evaluation.harbor.receipt import validate_calibration


class HarborReceiptTest(unittest.TestCase):
    def test_missing_artifact_is_incomplete_with_no_submitted_sha(self):
        grade = {"status": "incomplete_evidence", "reason": "missing_submission.bundle",
                 "submitted_sha": None, "checks": {}}
        validate_calibration("missing-output", grade, reward=0, submitted_sha=None)
        with self.assertRaises(ValueError):
            validate_calibration("missing-output", {**grade, "status": "rejected"}, 0, None)

    def test_regression_must_preserve_the_normal_path_and_fail_only_corruption(self):
        grade = {"status": "rejected", "reason": "corrupt", "submitted_sha": "a" * 40,
                 "checks": {"verified": True, "corrupt": False,
                            "missing_artifact": True, "unknown_run": True}}
        validate_calibration("regression", grade, 0, "a" * 40)
        with self.assertRaises(ValueError):
            validate_calibration("regression", {**grade, "checks": {}}, 0, "a" * 40)
        with self.assertRaises(ValueError):
            validate_calibration("regression", grade, 1, "a" * 40)
