from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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
        configuration = self.store.blob(encode({"factory_sha": build}))
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
            "run_id": run_id, "outcome_class": outcome_class, "approved_by": "fixture-reviewer",
            "outcome": "Fix the fixture", **changes,
        }, now=now)

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

    def test_expiry_ends_an_underfilled_cohort(self):
        self.registry.transition(self.activation, now=100)
        self.assertEqual(self.admit("window-expired", 150)["factory_digest"], self.baseline)
        self.assertEqual(self.registry.report(1)["admitted"], 0)
