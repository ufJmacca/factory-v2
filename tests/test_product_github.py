import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory.evidence import Conflict, Evidence
from test_product_delivery import OTHER, REVISION, request_for


class GithubConnectorTest(unittest.TestCase):
    def setUp(self):
        from factory.product_github import GitHub
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.request = request_for(Evidence(Path(directory.name)))
        self.provider = GitHub(None)
        self.provider.api = Mock()
        self.provider.git = Mock()
        self.pr = {
            "number": 4, "html_url": "https://github.com/example/widget/pull/4", "state": "open",
            "created_at": "2026-01-01T00:00:00Z", "body": "<!-- factory-run:run-a -->",
            "head": {"sha": REVISION, "ref": "codex/product/run-a", "repo": {"full_name": "example/widget"}},
            "base": {"ref": "main", "repo": {"full_name": "example/widget"}},
            "user": {"login": "author"},
        }

    def test_lookup_detects_marker_base_and_same_repository(self):
        self.provider.api.return_value = [[self.pr]]
        found = self.provider.find_pr(self.request)
        self.assertEqual((4, REVISION, "run-a"), (found["number"], found["revision"], found["marker"]))
        self.pr["base"]["ref"] = "other"
        with self.assertRaises(Conflict):
            self.provider.find_pr(self.request)
        self.pr["base"]["ref"] = "main"
        self.pr["body"] = "No run marker"
        with self.assertRaises(Conflict):
            self.provider.find_pr(self.request)
        self.pr["body"] = "<!-- factory-run:run-a -->"
        self.pr["head"]["repo"]["full_name"] = "other/widget"
        with self.assertRaises(Conflict):
            self.provider.find_pr(self.request)

    def test_ci_uses_current_status_and_review_records_keep_their_revision(self):
        checks = [{
            "id": 6, "name": "check", "head_sha": REVISION, "app": {"slug": "github-actions"},
            "status": "completed", "conclusion": "success",
        }]
        statuses = [[
            {"id": 5, "context": "suite", "state": "failure", "creator": {"login": "ci"}},
            {"id": 3, "context": "suite", "state": "success", "creator": {"login": "ci"}},
        ]]
        reviews = [[{
            "id": 2, "user": {"login": "reviewer"}, "commit_id": OTHER, "state": "APPROVED",
            "body": "", "submitted_at": "2026-01-01T00:00:10Z",
        }]]
        self.provider.api.side_effect = [self.pr, [{"check_runs": checks}], statuses, reviews, [], []]
        snapshot = self.provider.inspect(self.request, {"number": 4})
        self.assertEqual(REVISION, snapshot["revision"])
        self.assertEqual([("check", "success"), ("suite", "failure")],
                         [(item["name"], item["state"]) for item in snapshot["checks"]])
        self.assertEqual(OTHER, snapshot["review"]["revision"])

    def test_head_change_does_not_request_old_ci_or_review(self):
        self.pr["head"]["sha"] = OTHER
        self.provider.api.return_value = self.pr
        snapshot = self.provider.inspect(self.request, {"number": 4})
        self.assertEqual(OTHER, snapshot["revision"])
        self.provider.api.assert_called_once()


if __name__ == "__main__":
    unittest.main()
