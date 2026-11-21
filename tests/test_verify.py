from base64 import b64decode
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from benchmarks.records import build_fixtures


ROOT = Path(__file__).resolve().parents[1]
CHECKS = ROOT / "grading/checks.json"


class VerifyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundles = Path(cls.enterClassContext(tempfile.TemporaryDirectory()))
        cls.heads = build_fixtures(cls.bundles)
        cls.digest = sha256(CHECKS.read_bytes()).hexdigest()

    def verify(self, bundle, head, *, exit_code, reason, checked_sha, check_digest=None):
        result = subprocess.run(
            [
                sys.executable, "-I", str(ROOT / "src/factory/verify.py"),
                "--base", self.heads["base"],
                "--head", head,
                "--bundle", str(self.bundles / f"{bundle}.bundle"),
                "--checks", str(CHECKS),
                "--check-digest", check_digest if check_digest is not None else self.digest,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertTrue(result.stdout, result.stderr)
        verdict = json.loads(result.stdout)
        self.assertEqual(result.returncode, exit_code, verdict)
        self.assertEqual(verdict["decision"], "accepted" if exit_code == 0 else "rejected")
        self.assertEqual(verdict["reason"], reason)
        self.assertEqual(verdict["checked_sha"], checked_sha)
        self.assertEqual(verdict["check_digest"], self.digest)
        return verdict

    def output(self, check, channel):
        blob = check[channel]
        data = b64decode(blob["base64"], validate=True)
        self.assertEqual(blob["sha256"], sha256(data).hexdigest())
        return data

    def test_accepts_the_correct_revision(self):
        head = self.heads["correct"]
        verdict = self.verify(
            "correct", head, exit_code=0, reason="checks_passed", checked_sha=head,
        )
        outputs = {}
        for check in verdict["checks"]:
            self.assertTrue(check["passed"], check)
            self.assertEqual(check["exit_code"], 0)
            self.assertEqual(self.output(check, "stderr"), b"")
            outputs[check["id"]] = self.output(check, "stdout")
        self.assertEqual(outputs, {
            "final_record_without_newline": b"2\n",
            "empty_input": b"0\n",
            "newline_terminated": b"2\n",
        })

    def test_rejects_base_and_mutant_for_their_respective_regressions(self):
        for name, check_id, exit_code, stdout, stderr in (
            ("base", "final_record_without_newline", 0, b"1\n", b""),
            ("mutant", "empty_input", 1, b"", b"expected at least one record\n"),
        ):
            with self.subTest(revision=name):
                head = self.heads[name]
                verdict = self.verify(
                    name, head, exit_code=1, reason=f"check_failed:{check_id}", checked_sha=head,
                )
                failures = [check for check in verdict["checks"] if not check["passed"]]
                self.assertEqual([check["id"] for check in failures], [check_id])
                self.assertEqual(failures[0]["exit_code"], exit_code)
                self.assertEqual(self.output(failures[0], "stdout"), stdout)
                self.assertEqual(self.output(failures[0], "stderr"), stderr)

    def test_rejects_a_wrong_head_even_if_present_in_the_bundle(self):
        verdict = self.verify(
            "correct", self.heads["base"], exit_code=1, reason="head_mismatch", checked_sha=None,
        )
        self.assertEqual(verdict["checks"], [])

    def test_rejects_a_corrupt_pack_with_a_valid_bundle_header(self):
        bundle = (self.bundles / "correct.bundle").read_bytes()
        (self.bundles / "corrupt.bundle").write_bytes(bundle[:-1])
        verdict = self.verify(
            "corrupt", self.heads["correct"],
            exit_code=1, reason="invalid_bundle", checked_sha=None,
        )
        self.assertEqual(verdict["checks"], [])

    def test_rejects_checks_that_do_not_match_the_pin(self):
        verdict = self.verify(
            "correct", self.heads["correct"],
            exit_code=1, reason="check_digest_mismatch", checked_sha=None, check_digest="0" * 64,
        )
        self.assertEqual(verdict["checks"], [])
