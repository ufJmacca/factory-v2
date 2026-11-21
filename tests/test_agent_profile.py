from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory.agent_profile import InvalidProfile, resolve
from factory.evidence import Evidence, digest


ROOT = Path(__file__).resolve().parents[1]
PROFILE = """\
[factory]
schema_version = 1

[factory.bots.delivery-engineer]
on_event = "outcome.approved"
workflow = "deliver-outcome"
runtime_profile = "codex-cli-chatgpt"
model = "gpt-6.2-sol"
reasoning_effort = "medium"
instructions = "Deliver with tests."
skills = [".agents/skills/tdd", ".agents/skills/custom"]
tools = ["workspace.read", "workspace.edit", "workspace.test", "submission.create"]
timeout_minutes = 30
max_attempts = 2
"""


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE).decode().strip()


class ProfileTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.factory = root / "factory"
        self.factory.mkdir()
        self.store = Evidence(root / "evidence")
        git(self.factory, "init", "-b", "main")
        git(self.factory, "config", "user.name", "Fixture")
        git(self.factory, "config", "user.email", "fixture@example.invalid")
        self.skill("tdd", "SKILL.md", (ROOT / ".agents/skills/tdd/SKILL.md").read_text())
        self.skill("custom", "SKILL.md", "Read helper.txt for the custom method.")
        self.skill("custom", "helper.txt", "original helper")
        self.product = root / "product"
        (self.product / ".agents/skills/tdd").mkdir(parents=True)
        (self.product / ".agents/skills/tdd/SKILL.md").write_text("untrusted override")

    def skill(self, name, path, body):
        file = self.factory / ".agents/skills" / name / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(body)

    def commit(self, profile=PROFILE):
        (self.factory / "agents.toml").write_text(profile)
        git(self.factory, "add", ".")
        git(self.factory, "commit", "-m", "fixture configuration")
        return git(self.factory, "rev-parse", "HEAD")

    def test_skill_bytes_stay_pinned_when_factory_branch_and_product_differ(self):
        first = self.commit()
        resolved = resolve(self.factory, first, self.store)
        self.skill("custom", "helper.txt", "replacement helper")
        self.commit()
        self.assertEqual(resolve(self.factory, first, self.store), resolved)
        manifest = resolved["skills"]
        self.assertEqual(resolved["factory_sha"], first)
        self.assertEqual(manifest[".agents/skills/tdd/SKILL.md"],
                         digest((ROOT / ".agents/skills/tdd/SKILL.md").read_bytes()))
        self.assertEqual(self.store.read_blob(manifest[".agents/skills/custom/helper.txt"]), b"original helper")
        self.assertIn("workspace.test", resolved["tools"])

    def test_rejects_invalid_profiles_before_dispatch(self):
        for original, bad in (
            ("timeout_minutes = 30", "timeout_minuts = 30"),
            ('"codex-cli-chatgpt"', '"harness-openai-api"'),
            ('"workspace.test"', '"release.approve"'),
            ("max_attempts = 2", "max_attempts = true"),
            (".agents/skills/custom", ".agents/skills/unavailable"),
        ):
            with self.subTest(bad=bad):
                sha = self.commit(PROFILE.replace(original, bad))
                with self.assertRaises(InvalidProfile):
                    resolve(self.factory, sha, self.store)

    def test_untrusted_skill_links_are_rejected(self):
        (self.factory / ".agents/skills/custom/bad-link").symlink_to("/etc/passwd")
        with self.assertRaises(InvalidProfile):
            resolve(self.factory, self.commit(), self.store)


if __name__ == "__main__":
    unittest.main()
