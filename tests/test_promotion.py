from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory.codex_bridge import tool_definitions
from factory.evidence import Conflict, Evidence, digest, encode
from scripts.promotion import Promotion


class PromotionTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.store = Evidence(Path(directory))
        self.registry = Promotion(self.store)
        build = "a" * 40
        configuration = self.store.blob(encode({"factory_sha": build, "profile": {"max_attempts": 2}}))
        self.base_sha = "f" * 40
        self.checkpoint = self.store.blob(encode({"base_sha": self.base_sha, "head_sha": self.base_sha}))
        self.grader = self.store.blob(b"fixture grader")
        common = {
            "schema_version": 1, "factory_sha": build, "configuration": configuration,
            "runtime": {"workflow_build": build, "worker_image": "sha256:" + "1" * 64,
                        "transport": {"kind": "scripted-responses"}},
            "workspace_image": "sha256:" + "2" * 64, "source_inputs_sha256": "3" * 64,
        }

        def version(guidance):
            tools = tool_definitions(red_argv_guidance=guidance)
            return self.store.blob(encode({
                **common, "red_argv_guidance": guidance, "tool_definition_sha256": digest(encode(tools)),
            }))

        self.baseline = version(False)
        self.candidate = version(True)
        self.review = self.store.blob(encode({
            "schema_version": 1, "decision": "promote", "reviewer": "independent-maintainer",
            "baseline": self.baseline, "candidate": self.candidate, "tested_sha": build,
            "experiment_sha256": self.store.blob(b"fixture experiment"),
            "evidence_sha256": self.store.blob(b"fixture paired results"),
        }))
        self.parity = self.store.blob(encode({
            "schema_version": 1, "candidate": self.candidate, "tested_sha": build,
            "merged_sha": "b" * 40, "source_inputs_sha256": common["source_inputs_sha256"],
            "worker_image": common["runtime"]["worker_image"],
        }))
        self.initial = self.registry.initialize(self.baseline)
        self.scope = {
            "outcome_class": "offline-python-fix", "cohort": "fixtures", "max_runs": 2,
            "observation_seconds": 50,
            "limits": {"reopens": 0, "rollbacks": 0, "reviewer_seconds": 90, "cost_microusd": 500000},
        }
        self.activation = {
            "id": "activate-candidate", "action": "activate", "expected": self.initial,
            "target": self.candidate, "approved_by": "activation-operator", "expires_at": 200,
            "review": self.review, "parity": self.parity, "scope": self.scope,
        }

    def admit(self, run_id, now, outcome_class="offline-python-fix", **changes):
        return self.registry.admit({
            "schema_version": 1, "run_id": run_id, "outcome_class": outcome_class,
            "approved_by": "fixture-reviewer", "base_sha": self.base_sha,
            "base_checkpoint": self.checkpoint, "grader_digest": self.grader,
            "deadline_epoch": 300, "max_attempts": 1,
            "outcome": "Fix the fixture", **changes,
        }, now=now)

    def tool_schema(self, request):
        root = Path(__file__).resolve().parents[1]
        argv = [sys.executable, "-m", "factory.codex_bridge", str(self.store.root), request["run_id"],
                "fixture-workspace", self.base_sha]
        result = subprocess.run(
            argv, input=encode({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            env={**os.environ, "PYTHONPATH": str(root / "src")}, capture_output=True, check=True, timeout=10,
        )
        return json.loads(result.stdout)["result"]["tools"]

    def rollback(self, current, **changes):
        return {
            "id": "restore-baseline", "action": "rollback", "expected": current,
            "target": self.baseline, "approved_by": "activation-operator", "expires_at": 200,
            "reason": "Restore the approved baseline", **changes,
        }

    def test_activation_and_rollback_pin_only_new_eligible_requests(self):
        before = self.admit("in-flight", 99)
        active_receipt = self.registry.transition(self.activation, now=100)
        active = {"generation": 1, "digest": self.candidate}
        self.assertEqual(self.registry.active(), active)
        admitted = self.admit("candidate-run", 101)
        self.assertEqual((admitted["factory_digest"], admitted["red_argv_guidance"]), (self.candidate, True))
        self.assertEqual(self.admit("unrelated", 102, "documentation")["factory_digest"], self.baseline)
        self.assertEqual(self.admit("in-flight", 103), before)
        self.assertEqual(self.store.read("in-flight", "request")["factory_digest"], self.baseline)
        self.assertEqual(self.registry.transition(self.activation, now=201), active_receipt)

        rollback = self.rollback(active)
        rollback_receipt = self.registry.transition(rollback, now=104)
        self.assertEqual(self.registry.transition(rollback, now=201), rollback_receipt)
        self.assertEqual(self.registry.active(), {"generation": 2, "digest": self.baseline})
        self.assertEqual(self.admit("after-rollback", 105)["factory_digest"], self.baseline)
        self.assertEqual(self.admit("candidate-run", 106), admitted)
        self.assertEqual(self.registry.transition(self.activation, now=202), active_receipt)
        self.assertEqual(self.registry.active()["generation"], 2)
        baseline_schema = self.tool_schema(before)
        candidate_schema = self.tool_schema(admitted)
        self.assertEqual(baseline_schema, tool_definitions())
        self.assertEqual(candidate_schema, tool_definitions(red_argv_guidance=True))
        self.assertEqual(self.tool_schema(self.store.read("after-rollback", "request")), baseline_schema)

    def test_expired_mismatched_and_competing_approvals_leave_one_transition(self):
        for changed in (
            {"expires_at": 100},
            {"expected": {"generation": 0, "digest": self.candidate}},
            {"parity": self.store.blob(encode({"candidate": "foreign"}))},
            {"review": self.store.blob(encode({"decision": "hold"}))},
        ):
            with self.subTest(changed=changed), self.assertRaises(Conflict):
                self.registry.transition({**self.activation, **changed}, now=100)
        self.assertEqual(self.registry.active(), self.initial)

        def promote(operation_id):
            try:
                return self.registry.transition({**self.activation, "id": operation_id}, now=100)
            except Conflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(promote, ("competing-first", "competing-second")))
        self.assertEqual(sum(result is not None for result in results), 1)
        active = self.registry.active()
        self.assertEqual(active, {"generation": 1, "digest": self.candidate})
        self.registry.transition(self.rollback(active), now=101)
        with self.assertRaises(Conflict):
            self.registry.transition(self.activation, now=102)
        self.assertEqual(self.registry.active()["generation"], 2)

    def test_cohort_cap_window_and_admitted_identity(self):
        self.registry.transition(self.activation, now=100)
        first = self.admit("first", 101)
        self.assertEqual(self.admit("first", 102), first)
        with self.assertRaises(Conflict):
            self.admit("first", 102, outcome="A different outcome")
        with self.assertRaises(Conflict):
            self.admit("caller-supplied-arm", 102, red_argv_guidance=True)
        for index, changes in enumerate(({"max_attempts": 3}, {"deadline_epoch": 102}, {"base_sha": "d" * 40})):
            with self.subTest(changes=changes), self.assertRaises(Conflict):
                self.admit(f"invalid-delivery-{index}", 102, **changes)
        self.assertEqual(self.admit("second", 103)["factory_digest"], self.candidate)
        self.assertEqual(self.admit("over-cap", 104)["factory_digest"], self.baseline)
        self.assertEqual(self.admit("after-window", 150)["factory_digest"], self.baseline)
        self.assertEqual(self.registry.report(1)["admitted"], 2)

    def test_observations_record_limit_breaches_and_stop_new_candidate_admissions(self):
        self.registry.transition(self.activation, now=100)
        self.admit("watched", 101)
        event = {
            "id": "review-complete", "run_id": "watched", "observer": "reviewer",
            "metrics": {"reopens": 1, "rollbacks": 0, "reviewer_seconds": 91, "cost_microusd": None},
        }
        observation = self.registry.observe(event, now=102)
        self.assertEqual(self.registry.observe(event, now=151), observation)
        self.assertEqual(self.registry.report(1), {
            "generation": 1, "cohort": "fixtures", "admitted": 1, "observed": 1,
            "totals": {"reopens": 1, "rollbacks": 0, "reviewer_seconds": 91, "cost_microusd": 0},
            "unknown": {"reopens": 0, "rollbacks": 0, "reviewer_seconds": 0, "cost_microusd": 1},
            "exceeded": ["reopens", "reviewer_seconds"], "decision": "rollback_required",
        })
        self.assertEqual(self.admit("circuit-closed", 103)["factory_digest"], self.baseline)
        with self.assertRaises(Conflict):
            self.registry.observe({**event, "id": "late"}, now=150)
        with self.assertRaises(Conflict):
            self.registry.observe({**event, "id": "out-of-cohort", "run_id": "circuit-closed"}, now=104)

    def test_unknown_cost_prevents_an_all_clear(self):
        self.registry.transition(self.activation, now=100)
        self.admit("cost-watch", 101)
        self.assertEqual(self.registry.report(1)["decision"], "incomplete")
        self.registry.observe({
            "id": "usage", "run_id": "cost-watch", "observer": "reviewer",
            "metrics": {"reopens": 0, "rollbacks": 0, "reviewer_seconds": 30, "cost_microusd": None},
        }, now=103)
        self.assertEqual(self.registry.report(1)["decision"], "incomplete")
        self.registry.observe({
            "id": "invoice", "run_id": "cost-watch", "observer": "reviewer",
            "metrics": {"reopens": 0, "rollbacks": 0, "reviewer_seconds": 0, "cost_microusd": 500001},
        }, now=104)
        self.assertEqual(self.registry.report(1)["decision"], "rollback_required")

    def test_expiry_ends_an_underfilled_cohort(self):
        self.registry.transition(self.activation, now=100)
        self.assertEqual(self.admit("window-expired", 150)["factory_digest"], self.baseline)
        self.assertEqual(self.registry.report(1)["admitted"], 0)
