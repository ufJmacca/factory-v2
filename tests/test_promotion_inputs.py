from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory.agent_profile import resolve
from factory.checkpoint import git
from factory.codex_bridge import tool_definitions
from factory.evidence import Conflict, Evidence, digest, encode
from factory.harbor_receipt import runtime_pin
from scripts.promotion_inputs import compare_inputs, source_inputs, verify_merged


class PromotionInputsTest(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.repo = directory / "candidate"
        self.repo.mkdir()
        self.store = Evidence(directory / "evidence")
        git(self.repo, "init", "-q")
        source = Path(__file__).resolve().parents[1]
        (self.repo / "agents.toml").write_bytes((source / "agents.toml").read_bytes())
        for name in ("how", "tdd", "delivery-evidence"):
            skill = self.repo / ".agents" / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"{name} fixture")
        helper = self.repo / "src/factory/bridge.py"
        helper.parent.mkdir(parents=True)
        helper.write_text("# fixture helper\n")
        self.tested = self.commit("Tested inputs")
        self.worker = "sha256:" + "1" * 64
        profile = resolve(self.repo, self.tested, self.store)
        self.candidate = self.store.blob(encode({
            "schema_version": 1,
            "factory_sha": self.tested,
            "configuration": self.store.blob(encode(profile)),
            "runtime": runtime_pin(profile, self.tested, self.worker, {"kind": "scripted-responses"}),
            "workspace_image": "sha256:" + "2" * 64,
            "red_argv_guidance": True,
            "tool_definition_sha256": digest(encode(tool_definitions(red_argv_guidance=True))),
            "source_inputs_sha256": source_inputs(self.repo, self.tested),
        }))

    def commit(self, message):
        git(self.repo, "add", "--all")
        git(self.repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@invalid", "commit", "-qm", message)
        return git(self.repo, "rev-parse", "HEAD").decode().strip()

    def test_comparison_accepts_control_plane_changes_with_identical_worker(self):
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts/promotion.py").write_text("# independent admission control\n")
        head = self.commit("Add an admission control")
        comparison = compare_inputs(self.store, self.repo, self.candidate, head, self.worker)
        self.assertEqual(comparison["tested_sha"], self.tested)
        self.assertEqual(comparison["reviewed_sha"], head)
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.tested)
        with self.assertRaises(Conflict):
            verify_merged(self.store, self.repo, self.candidate, head, self.worker)
        git(self.repo, "update-ref", "refs/remotes/origin/main", head)
        proof = verify_merged(self.store, self.repo, self.candidate, head, self.worker)
        self.assertEqual(proof["merged_sha"], head)

    def test_mismatched_image_skill_selector_or_helper_blocks_merge_parity(self):
        with self.assertRaises(Conflict):
            compare_inputs(self.store, self.repo, self.candidate, self.tested, "sha256:" + "3" * 64)
        for path in ("src/factory/bridge.py", ".agents/skills/how/SKILL.md", "agents.toml"):
            with self.subTest(path=path):
                file = self.repo / path
                original = file.read_bytes()
                file.write_bytes(original + b"\n# changed input\n")
                changed = self.commit(f"Change {path}")
                with self.assertRaises(Conflict):
                    compare_inputs(self.store, self.repo, self.candidate, changed, self.worker)
                file.write_bytes(original)
                self.commit(f"Restore {path}")
