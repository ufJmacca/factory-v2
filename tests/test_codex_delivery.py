from contextlib import contextmanager
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import time
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory.codex_delivery import dispatch
from factory.evidence import Evidence, encode
from factory.harbor_binding import Binding


class FixtureWorkspace:
    def __init__(self, name):
        self.name = name

    @contextmanager
    def provision(self, image, snapshot):
        yield self

    def run(self, argv):
        return {"exit_code": 0, "output": "/work/product", "truncated": False}


class DispatchTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Evidence(self.root)
        self.profile = {
            "profile": {"instructions": "Run the check."},
            "source_digest": self.store.blob(b"original profile"),
            "skills": {".agents/skills/custom/helper.txt": self.store.blob(b"original helper")},
        }
        self.request = {
            "run_id": "fixture", "outcome": "bounded task", "deadline_epoch": time() + 15,
            "configuration": self.store.blob(encode(self.profile)),
            "base_checkpoint": self.store.blob(encode({"head_sha": "base"})),
            "workspace_image": "fixture",
        }

    def dispatch(self, attempt, script):
        with patch("factory.codex_delivery.Workspace", FixtureWorkspace), \
                patch("factory.codex_delivery.preflight", return_value=None), \
                patch("factory.codex_delivery.cli_args", return_value=[sys.executable, "-c", script]):
            return dispatch(self.store, self.request, {"attempt": attempt}, "fixture", self.root)

    def test_duplicate_dispatch_replays_receipt_and_retry_reuses_evidence(self):
        one = self.dispatch(1, """
import json, os, signal, sys
print(json.dumps({"type":"item.completed","item":{"text":sys.stdin.read()}}), flush=True)
os.kill(os.getpid(), signal.SIGKILL)
""")
        self.assertEqual(one["reason"], "interrupted")
        self.assertEqual(self.dispatch(1, "raise RuntimeError('duplicate launched')"), one)
        two = self.dispatch(2, """
import json, sys
print(json.dumps({"type":"item.completed","item":{"text":sys.stdin.read()}}))
""")
        self.assertEqual(two["reason"], "missing_submission")
        self.assertEqual(one["configuration"], two["configuration"])
        for result in (one, two):
            data = self.store.read_blob(result["native_events"])
            self.assertIn("original helper", json.loads(data)["item"]["text"])
        self.assertFalse((self.root / "active-codex-run.json").exists())

    def test_abandoned_dispatch_stops_without_starting_another_process(self):
        self.store.record("fixture", "attempt-1-intent", {"attempt": 1})
        self.addCleanup((self.root / "active-codex-run.json").unlink, missing_ok=True)
        (self.root / "active-codex-run.json").touch()
        result = self.dispatch(2, "raise RuntimeError('unfenced process launched')")
        self.assertEqual(result["reason"], "unfenced_prior_attempt")
        self.assertNotIn("native_events", result)

    def test_missing_pinned_skill_stops_before_dispatch_with_explicit_reason(self):
        [reference] = self.profile["skills"].values()
        self.store.path(reference).unlink()
        result = self.dispatch(1, "raise RuntimeError('configuration not pinned')")
        self.assertEqual(result["reason"], "pinned_configuration_unavailable")
        self.assertNotIn("native_events", result)

    def test_scoped_delivery_cannot_pass_with_only_a_postflight_marker(self):
        binding = Binding(self.root, str(uuid4()))
        request = {**self.request, "run_id": binding.name, "base_sha": "base",
                   "workspace_marker": "trusted-round-trip", "evaluation": {"rollout": binding.rollout}}

        def fake_run_cli(args, cwd, env, text, events, deadline, **kwargs):
            self.assertIn(b"workspace tool for every product command", text)
            events.write_text("")
            self.store.record(binding.name, "submission", {"head_sha": "submitted"})
            return {"reason": "completed"}

        with patch("factory.codex_delivery.preflight", return_value=None), \
                patch("factory.codex_delivery.cli_args", return_value=["fake-cli"]), \
                patch("factory.codex_delivery.run_cli", side_effect=fake_run_cli), \
                patch.object(Binding, "run", return_value={"exit_code": 0, "output": binding.name, "truncated": False}):
            result = dispatch(self.store, request, {"attempt": 1}, "fixture", self.root, binding=binding)
            self.assertEqual(result["reason"], "workspace_marker_missing")
            self.assertEqual(dispatch(self.store, request, {"attempt": 1}, "fixture", self.root, binding=binding), result)
        self.assertFalse(self.store.read(binding.name, "attempt-1-marker")["assigned_workspace"])
        self.assertIsNone(self.store.read(binding.name, "attempt-1-workspace-proof"))


if __name__ == "__main__":
    unittest.main()
