import asyncio
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory.codex_bridge import Bridge, WorkspaceProof
from factory.evidence import Conflict, Evidence, encode
from factory.harbor_binding import Binding, LeaseLost


class BindingTests(unittest.TestCase):
    def test_rollouts_receive_only_their_own_commands(self):
        with TemporaryDirectory() as directory:
            left, right = [Binding(Path(directory), str(uuid4())) for _ in range(2)]
            for binding in (left, right):
                binding.open()
            responses = []
            client = Thread(target=lambda: responses.append(left.run(["printf", "left"])))
            client.start()
            deadline = monotonic() + 2
            while (call := left.next_call()) is None and monotonic() < deadline:
                sleep(0.005)
            try:
                self.assertIsNone(right.next_call())
                self.assertEqual(call[1], ["printf", "left"])
                left.reply(call[0], {"exit_code": 0, "output": "left", "truncated": False})
            finally:
                client.join(timeout=3)
            self.assertEqual(responses, [{"exit_code": 0, "output": "left", "truncated": False}])

    def test_revocation_rejects_pending_commands_and_reopening(self):
        with TemporaryDirectory() as directory:
            binding = Binding(Path(directory), str(uuid4()))
            binding.open()
            binding.revoke("cancelled")
            for operation in (binding.open, binding.pulse, lambda: binding.run(["true"])):
                with self.assertRaises(LeaseLost):
                    operation()
            self.assertIsNone(binding.next_call())

    def test_expired_lease_cannot_be_revived(self):
        with TemporaryDirectory() as directory:
            binding = Binding(Path(directory), str(uuid4()), lease_seconds=0.01)
            binding.open()
            sleep(0.03)
            self.assertFalse(binding.active())
            with self.assertRaises(LeaseLost):
                binding.pulse()

    def test_rollout_id_is_canonical(self):
        with self.assertRaises(ValueError):
            Binding(Path("/tmp"), "../another-run")


class ShellEnvironment:
    def __init__(self):
        self.calls = []
        self.wrong_cwd = None
        self.rewrite_stdout = str

    async def exec(self, *, command, cwd, user, timeout_sec):
        self.calls.append(command)
        result = subprocess.run(["/bin/bash", "-c", command], cwd=self.wrong_cwd or cwd,
                                capture_output=True, text=True, timeout=5)
        return SimpleNamespace(return_code=result.returncode, stdout=self.rewrite_stdout(result.stdout), stderr=result.stderr)


class WorkspaceProofTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.checkout = root / "product"
        (self.checkout / ".git").mkdir(parents=True)
        self.marker = self.checkout / ".git/factory-marker"
        self.store = Evidence(root / "evidence")
        self.binding = Binding(self.store.root, str(uuid4()))
        self.binding.open()
        self.run_id = self.binding.name
        self.container = subprocess.check_output(["hostname"], text=True).strip()
        self.store.record(self.run_id, "harbor-ready", {
            "rollout": self.binding.rollout, "container": self.container,
        })
        self.bridge = Bridge(self.store, self.run_id, self.binding, "base", proof_attempt=1)
        self.environment = ShellEnvironment()
        self.ended = Event()
        self.addCleanup(self.ended.set)
        self.workspace_cwd = patch("factory.harbor_binding.WORKSPACE_CWD", str(self.checkout))
        self.workspace_cwd.start()
        self.addCleanup(self.workspace_cwd.stop)
        self.errors = []
        provider = Thread(target=self.provider, daemon=True)
        provider.start()
        self.addCleanup(self.stop_provider, provider)

    def stop_provider(self, provider):
        self.ended.set()
        provider.join(timeout=5)
        self.assertEqual(self.errors, [])

    def provider(self):
        try:
            while not self.ended.is_set():
                if call := self.binding.next_call():
                    result = asyncio.run(self.binding.execute(self.environment, call, self.container))
                    self.binding.reply(call[0], result)
                else:
                    self.ended.wait(0.005)
        except Exception as error:
            self.errors.append(error)

    def command(self, command):
        return self.bridge.call("workspace", {"command": command})

    def proof(self):
        return self.store.read(self.run_id, "attempt-1-workspace-proof")

    def test_schema_rejection_cannot_start_workspace_proof(self):
        with self.assertRaisesRegex(Conflict, "invalid workspace command"):
            self.bridge.call("workspace", {"command": "true", "extra": 1})
        self.assertIsNone(self.proof())
        self.assertEqual(self.environment.calls, [])
        self.assertFalse(self.marker.exists())

    def test_valid_workspace_call_returns_shell_result_and_retains_provider_receipts(self):
        self.assertEqual(self.command("printf 'shell\n'; printf error >&2; exit 7"), {
            "exit_code": 7, "output": "shell\nerror", "truncated": False,
        })
        proof = self.proof()
        self.assertEqual(proof["marker"], self.marker.read_text())
        self.assertEqual(proof["marker"], self.run_id + ":1")
        self.assertEqual(proof["rollout"], self.binding.rollout)
        self.assertEqual(proof["attempt"], 1)
        for key, operation in [("command", "workspace-command"), ("write", "marker-write"), ("read", "marker-read")]:
            receipt = proof[key + "_receipt"]
            self.assertEqual(receipt["rollout"], self.binding.rollout)
            self.assertEqual(receipt["attempt"], 1)
            self.assertEqual(receipt["operation"], operation)
            self.assertEqual(receipt["container"], self.container)
            self.assertEqual(receipt["cwd"], str(self.checkout))
        self.assertEqual(self.command("printf second"), {"exit_code": 0, "output": "second", "truncated": False})
        self.assertEqual(self.proof(), proof)
        self.assertEqual(len(self.environment.calls), 4)

    def test_wrong_command_workspace_cannot_be_masked_by_a_correct_marker_workspace(self):
        self.environment.wrong_cwd = self.checkout.parent
        with self.assertRaisesRegex(Conflict, "workspace receipt identity"):
            self.command("touch ran")
        self.environment.wrong_cwd = None
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.checkout.parent / "ran").exists())
        self.assertIsNone(self.proof())
        self.assertEqual(len(self.environment.calls), 1)

    def test_command_receipt_from_another_attempt_cannot_establish_proof(self):
        self.environment.rewrite_stdout = lambda text: text.replace("\t1\tworkspace-command\t", "\t2\tworkspace-command\t", 1)
        with self.assertRaisesRegex(Conflict, "workspace receipt identity"):
            self.command("printf shell")
        self.assertIsNone(self.proof())
        self.assertFalse(self.marker.exists())

    def test_existing_marker_cannot_be_claimed_or_replaced(self):
        self.marker.write_text("eval-other:1")
        with self.assertRaisesRegex(Conflict, "workspace marker"):
            self.command("printf shell")
        self.assertIsNone(self.proof())
        self.assertEqual(self.marker.read_text(), "eval-other:1")

    def test_recovers_first_reply_without_repeating_command_or_marker_write(self):
        command = "printf side-effect >> .git/command-log; printf original"
        record = self.store.record

        def lose_proof_reply(run_id, key, value):
            if key == "attempt-1-workspace-proof":
                raise OSError("lost before durable proof")
            return record(run_id, key, value)

        with patch.object(self.store, "record", side_effect=lose_proof_reply):
            with self.assertRaisesRegex(OSError, "lost before durable proof"):
                self.command(command)
        self.assertIsNone(self.proof())
        self.assertEqual(self.marker.read_text(), self.run_id + ":1")
        self.assertEqual(self.command(command), {"exit_code": 0, "output": "original", "truncated": False})
        self.assertEqual((self.checkout / ".git/command-log").read_text(), "side-effect")
        self.assertEqual(len(self.environment.calls), 3)
        self.assertIsNotNone(self.proof())

    def test_postflight_rejects_removed_stale_or_foreign_marker_without_restoring_it(self):
        self.command("true")
        proof = self.proof()
        verifier = WorkspaceProof(self.store, self.binding, 1)
        verified = verifier.postflight()
        self.assertTrue(verified["assigned_workspace"])
        self.assertEqual(self.store.read_blob(verified["workspace_proof"]), encode(proof))
        self.assertEqual(verified["postflight_receipt"]["operation"], "marker-postflight")

        self.command("rm .git/factory-marker")
        self.assertFalse(verifier.postflight()["assigned_workspace"])
        self.assertFalse(self.marker.exists())
        for invalid in (self.run_id + ":0", "eval-other:1", proof["marker"] + "\n"):
            with self.subTest(invalid=invalid):
                self.marker.write_text(invalid)
                self.assertFalse(verifier.postflight()["assigned_workspace"])
                self.assertEqual(self.marker.read_text(), invalid)
        self.assertEqual(self.proof(), proof)


if __name__ == "__main__":
    unittest.main()
