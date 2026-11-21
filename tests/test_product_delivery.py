import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory.evidence import Conflict, Evidence, digest, encode
from factory.product import branch


REVISION = "1" * 40
OTHER = "2" * 40


class ProductFixture:
    def __init__(self, store, *, lose_pr=False):
        self.store = store
        self.rev = None
        self.pr = None
        self.state = "open"
        self.writes = []
        self.lose_pr = lose_pr
        self.checks = []
        self.review = None

    def find_push(self, request):
        if self.rev:
            return {"ref": "refs/heads/" + branch(request), "revision": self.rev}

    def push(self, request):
        self.assert_intent(request, "push")
        self.writes.append("push")
        self.rev = request["revision"]

    def find_pr(self, request):
        return {**self.pr, "revision": self.rev, "state": self.state} if self.pr else None

    def create_pr(self, request):
        self.assert_intent(request, "pull_request")
        self.writes.append("pull_request")
        self.pr = {
            "number": 4, "url": "https://github.com/example/widget/pull/4", "created_epoch": 10,
            "revision": self.rev, "base": request["base"], "repo": request["repo"],
            "head_ref": "codex/product/" + request["run_id"], "marker": request["run_id"],
        }
        if self.lose_pr:
            raise TimeoutError("fixture dropped the successful response")

    def inspect(self, request, pr):
        return {"revision": self.rev, "state": self.state, "checks": self.checks,
                "review": self.review, "corrections": []}

    def assert_intent(self, request, action):
        intent = self.store.read(request["run_id"], "intent-" + action)
        assert intent["operation_id"]
        assert intent["request_digest"] == digest(encode(intent["request"]))
        assert self.store.read(request["run_id"], "attempt-" + action)

    def verify(self):
        self.checks = [{"kind": "status", "name": "suite", "actor": "ci",
                        "revision": REVISION, "state": "success", "id": 1}]
        self.review = {"actor": "reviewer", "revision": REVISION, "state": "approved", "id": 2}


def request_for(store, *, run_id="run-a", environment=None):
    return {
        "schema_version": 1, "run_id": run_id, "repo": "example/widget", "base": "main",
        "revision": REVISION, "approver": "maintainer", "reviewer": "reviewer",
        "checks": [{"kind": "status", "name": "suite", "actor": "ci"}],
        "deadline": 50, "observation_seconds": 5, "environment": environment,
        "source": {"independent_verdict": store.blob(encode({"checked_sha": REVISION, "decision": "accepted"}))},
    }


def grant(store, request, action, **changes):
    from factory.product import record_approval
    record_approval(store, request["run_id"], {
        "actor": request["approver"], "action": action, "repo": request["repo"],
        "environment": request["environment"] if action == "deploy" else None,
        "revision": request["revision"], "expires_at": 40, "run_id": request["run_id"],
        **changes,
    })


class ProductDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Evidence(Path(self.directory.name) / "runs-data")

    def runner(self, request=None, remote=None):
        from factory.product import FixtureDeployment, ProductRunner
        self.request = request or request_for(self.store)
        self.store.admit(self.request)
        self.remote = remote or ProductFixture(self.store, lose_pr=True)
        self.deploy = FixtureDeployment(Evidence(Path(self.directory.name) / "deployments"))
        return ProductRunner(self.store, self.remote, self.deploy, clock=lambda: self.now)

    def authorize(self):
        grant(self.store, self.request, "push")
        grant(self.store, self.request, "pull_request")

    def advance(self, runner, when=11):
        self.now = when
        return runner.advance(self.request["run_id"])

    def test_reconciles_lost_pr_response_on_duplicate_submission_and_reopens(self):
        from factory.product import reopen_product
        runner = self.runner()
        self.authorize()
        self.assertEqual("awaiting_checks", self.advance(runner)["status"])
        self.store.admit(self.request)
        self.remote.verify()
        self.assertEqual("observed", self.advance(runner, 16)["status"])
        self.assertEqual("observed", self.advance(runner, 17)["status"])
        self.assertEqual(["push", "pull_request"], self.remote.writes)
        result = reopen_product(self.store, self.request["run_id"])
        self.assertEqual(4, result["pull_request"]["number"])
        self.assertEqual(REVISION, result["accepted_revision"])
        self.assertEqual("unknown", result["active_human_minutes"])
        self.assertEqual("reconciled", self.store.read("run-a", "receipt-pull_request")["receipt_source"])
        self.store.path(result["artifacts"]["receipt-pull_request"]).write_bytes(b"corrupt")
        with self.assertRaises(Conflict):
            reopen_product(self.store, "run-a")

    def test_reopen_rejects_an_edited_observation_summary(self):
        from factory.product import record_observation, reopen_product
        runner = self.runner()
        self.authorize()
        self.advance(runner)
        record_observation(self.store, "run-a", {
            "actor": "maintainer", "pull_request": 4, "revision": REVISION,
            "observed_at": 14, "feedback": "Verified the expected output", "active_human_minutes": 3,
        })
        self.remote.verify()
        self.advance(runner, 16)
        result = reopen_product(self.store, "run-a")
        self.assertEqual(3, result["active_human_minutes"])
        self.assertEqual("Verified the expected output", result["feedback"][0]["feedback"])
        report_file = self.store.run_dir("run-a") / "product-manifest.json"
        document = json.loads(report_file.read_bytes())
        document["active_human_minutes"] = 9
        report_file.write_bytes(encode(document))
        with self.assertRaises(Conflict):
            reopen_product(self.store, "run-a")

    def test_wrong_scope_and_expired_approvals_leave_run_waiting_until_deadline(self):
        runner = self.runner()
        for changes in ({"actor": "someone-else"}, {"repo": "example/other"},
                        {"revision": OTHER}, {"expires_at": 11}, {"environment": "other"}):
            grant(self.store, self.request, "push", **changes)
        grant(self.store, self.request, "pull_request")
        self.assertEqual("awaiting_push_approval", self.advance(runner)["status"])
        self.assertEqual([], self.remote.writes)
        closed = self.advance(runner, 50)
        self.assertEqual("unshipped", closed["status"])
        self.assertEqual("deadline", closed["reason"])

    def test_changed_head_invalidates_green_checks_review_and_deployment_approval(self):
        runner = self.runner(request_for(self.store, environment="preview"))
        self.authorize()
        self.advance(runner)
        self.remote.verify()
        self.assertEqual("awaiting_deploy_approval", self.advance(runner, 12)["status"])
        grant(self.store, self.request, "deploy")
        self.remote.rev = OTHER
        closed = self.advance(runner, 13)
        self.assertEqual("head_changed", closed["reason"])
        self.assertIsNone(closed["accepted_revision"])
        self.assertIsNone(closed["deployment"])

    def test_old_or_untrusted_ci_and_review_do_not_satisfy_gates(self):
        runner = self.runner()
        self.authorize()
        self.advance(runner)
        self.remote.verify()
        self.remote.checks[0]["actor"] = "other-ci"
        self.assertEqual("awaiting_checks", self.advance(runner, 12)["status"])
        self.remote.verify()
        self.remote.review["revision"] = OTHER
        self.assertEqual("awaiting_review", self.advance(runner, 13)["status"])

    def test_deployment_fixture_reconciles_by_repo_revision_and_environment(self):
        for run_id, environment in (("run-a", "preview"), ("run-b", "preview"), ("run-c", "qa")):
            runner = self.runner(request_for(self.store, run_id=run_id, environment=environment))
            self.authorize()
            self.advance(runner)
            self.remote.verify()
            grant(self.store, self.request, "deploy", environment="wrong")
            self.assertEqual("awaiting_deploy_approval", self.advance(runner, 15)["status"])
            grant(self.store, self.request, "deploy")
            result = self.advance(runner, 16)
            self.assertEqual("observed", result["status"])
            self.assertEqual(environment, result["deployment"]["environment"])
            self.assertEqual(REVISION, result["deployment"]["revision"])
        self.assertEqual(2, len(list((self.deploy.store.root / "runs").glob("*/deployment.json"))))

    def test_integrated_revision_needs_fresh_approval_and_reuses_original_pr(self):
        runner = self.runner()
        self.authorize()
        self.advance(runner)
        self.remote.verify()
        prior = self.advance(runner, 16)
        request = request_for(self.store, run_id="run-b")
        request.update(
            revision=OTHER,
            predecessor={"run_id": "run-a", "revision": REVISION, "base_sha": "3" * 40},
            source={"prior_product_manifest": self.store.blob(encode(prior)),
                    "integration_verdict": self.store.blob(encode({"checked_sha": OTHER, "decision": "accepted"}))},
        )
        runner = self.runner(request, self.remote)
        grant(self.store, request, "push", revision=REVISION)
        self.assertEqual("awaiting_push_approval", self.advance(runner, 17)["status"])
        grant(self.store, request, "push")
        self.remote.checks[0]["revision"] = OTHER
        self.remote.review["revision"] = OTHER
        self.assertEqual("observing", self.advance(runner, 18)["status"])
        result = self.advance(runner, 23)
        self.assertEqual(OTHER, result["accepted_revision"])
        self.assertEqual(4, result["pull_request"]["number"])
        self.assertEqual([18, 23], result["observation_window"])
        self.assertEqual(["push", "pull_request", "push"], self.remote.writes)

    def test_integration_does_not_update_the_branch_of_a_closed_pr(self):
        runner = self.runner()
        self.authorize()
        self.advance(runner)
        self.remote.verify()
        prior = self.advance(runner, 16)
        request = request_for(self.store, run_id="run-b")
        request.update(
            revision=OTHER,
            predecessor={"run_id": "run-a", "revision": REVISION, "base_sha": "3" * 40},
            source={"prior_product_manifest": self.store.blob(encode(prior)),
                    "integration_verdict": self.store.blob(encode({"checked_sha": OTHER, "decision": "accepted"}))},
        )
        runner = self.runner(request, self.remote)
        grant(self.store, request, "push")
        self.remote.state = "closed"
        self.assertEqual("unshipped", self.advance(runner, 17)["status"])
        self.assertEqual(REVISION, self.remote.rev)
        self.assertEqual(["push", "pull_request"], self.remote.writes)

    def test_conflicting_existing_branch_stops_before_pr_creation(self):
        runner = self.runner()
        self.authorize()
        self.remote.rev = OTHER
        self.assertEqual("unshipped", self.advance(runner)["status"])
        self.assertEqual([], self.remote.writes)


if __name__ == "__main__":
    unittest.main()
