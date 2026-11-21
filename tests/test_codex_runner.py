from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory.codex_runner import run_cli
from factory.evidence import Evidence


class CliTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def run_script(self, code, seconds=4):
        return run_cli(
            [sys.executable, "-c", code], self.root, {}, b"test prompt",
            self.root / "events.jsonl", time.time() + seconds,
        )

    def test_captures_native_events_and_usage_without_raw_secrets(self):
        result = self.run_script("""
import json
print(json.dumps({"type":"thread.started", "thread_id":"fixture"}))
print(json.dumps({"type":"error", "message":"sk-proj-should-not-be-retained"}))
print(json.dumps({"type":"turn.completed", "usage":{"input_tokens":21, "output_tokens":5}}))
""")
        self.assertEqual(result["reason"], "completed")
        self.assertEqual(result["usage"]["input_tokens"], 21)
        self.assertIsNone(result["billing"])
        stream = (self.root / "events.jsonl").read_text()
        self.assertIn("turn.completed", stream)
        self.assertNotIn("should-not-be-retained", stream)

    def test_preserves_partial_events_and_fences_an_interrupted_process(self):
        result = self.run_script("""
import os
import signal
print('{"type":"thread.started","thread_id":"fixture"}', flush=True)
os.kill(os.getpid(), signal.SIGKILL)
""")
        self.assertEqual(result["reason"], "interrupted")
        self.assertIn("thread.started", (self.root / "events.jsonl").read_text())

    def test_stops_at_deadline(self):
        result = self.run_script("import time; time.sleep(30)", seconds=0.2)
        self.assertEqual(result["reason"], "deadline_exceeded")

    def test_elapsed_deadline_does_not_launch_the_cli(self):
        result = run_cli(
            [str(self.root / "should-not-be-launched")], self.root, {}, b"",
            self.root / "events.jsonl", time.time() - 1,
        )
        self.assertEqual(result["reason"], "deadline_exceeded")
        self.assertIsNone(result["exit_code"])

    def test_explicit_subscription_failures_and_no_fallback(self):
        for index, (message, reason) in enumerate((
            ("You have hit your usage limit", "quota_exhausted"),
            ("Your access token could not be refreshed. Log out and sign in again.", "login_required"),
        )):
            with self.subTest(reason=reason):
                result = run_cli(
                    [sys.executable, "-c", f"import sys; sys.stderr.write({message!r});sys.exit(1)"],
                    self.root, {}, b"", self.root / f"events-{index}.jsonl", time.time() + 4,
                )
                self.assertEqual(result["reason"], reason)
                self.assertIsNone(result["usage"])
                self.assertIsNone(result["billing"])


if __name__ == "__main__":
    unittest.main()
