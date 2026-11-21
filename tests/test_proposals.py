import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory.evidence import Conflict, digest, encode
from factory.proposal_evidence import ReadOnlyArchive, workspace_retry
from factory.proposals import candidate_patch, publish_proposal


def completed(item_id, arguments, result):
    return encode({
        "type": "item.completed",
        "item": {
            "id": item_id,
            "type": "mcp_tool_call",
            "server": "factory",
            "tool": "workspace",
            "arguments": arguments,
            "result": {"content": [{"type": "text", "text": result}]},
            "error": None,
        },
    })


class ProposalTest(unittest.TestCase):
    def test_verified_blob_bytes_are_read_only(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "blobs").mkdir()
            archive = ReadOnlyArchive(root)
            content = b"archive fixture"
            blob = root / "blobs" / digest(content)
            blob.write_bytes(content)
            self.assertEqual(archive.read_blob(blob.name), content)
            blob.write_bytes(b"changed")
            with self.assertRaises(Conflict):
                archive.read_blob(blob.name)
            with self.assertRaises(Conflict):
                archive.read_blob("../not-a-digest")
            self.assertFalse(hasattr(archive, "record"))
            self.assertFalse(hasattr(archive, "blob"))

    def test_retry_changes_only_an_extra_argument(self):
        command = "cat tests/test_recovery.py"
        failed = completed("item_9", {"command": command, "a": 1}, "invalid workspace command")

        def retry(new_command, exit_code):
            result = {"exit_code": exit_code, "output": "fixture", "truncated": False}
            return completed("item_10", {"command": new_command}, json.dumps(result))

        pair = workspace_retry(failed + retry(command, 0), 1, 2)
        self.assertEqual([(call.line, call.item_id) for call in pair], [(1, "item_9"), (2, "item_10")])
        self.assertIsNone(workspace_retry(failed + retry("git status", 0), 1, 2))
        self.assertIsNone(workspace_retry(failed + retry(command, 1), 1, 2))
        self.assertIsNone(workspace_retry(failed + retry(command, 0), 2, 1))

    def test_candidate_changes_one_skill_and_leaves_its_source_alone(self):
        before = b"---\nname: delivery-evidence\n---\n\nExisting instruction.\n"
        patch, after = candidate_patch(before)
        self.assertEqual([line for line in patch.splitlines() if line.startswith(("--- ", "+++ "))], [
            "--- a/.agents/skills/delivery-evidence/SKILL.md",
            "+++ b/.agents/skills/delivery-evidence/SKILL.md",
        ])
        self.assertTrue(after.startswith(before))
        self.assertIn(b'`command`', after)
        self.assertNotIn(b'`command`', before)

    def test_publish_deduplicates_and_refuses_to_replace_a_finding(self):
        result = {"proposal": {"id": "p05-" + "a" * 64}, "candidate": {"diff": "example"}}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = publish_proposal(root, result)
            self.assertEqual(publish_proposal(root, result), path)
            self.assertEqual(list(root.iterdir()), [path])
            self.assertEqual(json.loads(path.read_bytes()), result)
            with self.assertRaises(Conflict):
                publish_proposal(root, {**result, "candidate": {"diff": "different"}})
            self.assertEqual(json.loads(path.read_bytes()), result)
