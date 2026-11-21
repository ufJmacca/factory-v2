from copy import deepcopy
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory.evidence import Conflict, Evidence
from factory.harbor_experiment import assignment_plan, compare_profiles, report, terminal


class HarborExperimentTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Evidence(Path(directory.name))
        self.plan = assignment_plan("phase08", ["verified-patch", "profile-preview", "rpc-unknown-operation"], 3)

    def test_pairs_have_both_arms_and_order_is_reproducible(self):
        self.assertEqual(len(self.plan), 18)
        self.assertEqual(self.plan, assignment_plan(
            "phase08", ["verified-patch", "profile-preview", "rpc-unknown-operation"], 3))
        for i in range(0, 18, 2):
            left, right = self.plan[i:i + 2]
            self.assertEqual((left["task"], left["pair"]), (right["task"], right["pair"]))
            self.assertEqual({left["arm"], right["arm"]}, {"baseline", "candidate"})

    def test_profile_comparison_rejects_other_input_changes(self):
        baseline = {"factory_sha": "base", "skills": {"skill": "old", "other": "fixed"}, "tools": ["read"]}
        candidate = deepcopy(baseline)
        candidate.update(factory_sha="candidate")
        candidate["skills"]["skill"] = "new"
        compare_profiles(baseline, candidate, "skill", "old", "new")
        candidate["skills"]["other"] = "changed"
        with self.assertRaises(Conflict):
            compare_profiles(baseline, candidate, "skill", "old", "new")

    def test_stopped_and_rejected_assignments_remain_in_denominator(self):
        row = self.plan[0]
        terminal(self.store, row, {"disposition": "rejected", "attempt": {
            "job": "job", "cost_usd": None, "scored": True, "accepted": False,
        }})
        terminal(self.store, self.plan[1], {"disposition": "stopped", "attempt": None})
        result = report(self.store, self.plan)
        self.assertEqual((result["planned"], result["attempts"], result["scored"], result["accepted"]),
                         (18, 1, 1, 0))
        self.assertEqual(result["cost"]["unknown_attempts"], 1)
        self.assertIsNone(result["cost"]["per_accepted_usd"])
        self.assertEqual(result["decision"], "hold")

    def test_attempt_and_disposition_cannot_be_overwritten(self):
        row = self.plan[0]
        stopped = {"disposition": "stopped", "attempt": None}
        self.assertEqual(terminal(self.store, row, stopped), terminal(self.store, row, stopped))
        with self.assertRaises(Conflict):
            terminal(self.store, row, {"disposition": "accepted", "attempt": {"job": "other"}})
