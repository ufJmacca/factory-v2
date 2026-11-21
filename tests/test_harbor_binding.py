from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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


if __name__ == "__main__":
    unittest.main()
