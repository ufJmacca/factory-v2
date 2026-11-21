from copy import deepcopy
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory.evidence import Conflict, Evidence, encode
from factory.harbor_experiment import assignment_plan, compare_profiles, report, terminal, workspace_rejections


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
            "job": "job", "cost_usd": 1.25, "scored": True, "accepted": False,
        }})
        terminal(self.store, self.plan[1], {"disposition": "invalid_grade", "attempt": {
            "job": "second", "cost_usd": None, "scored": False, "accepted": False,
        }})
        terminal(self.store, self.plan[2], {"disposition": "stopped", "attempt": None})
        result = report(self.store, self.plan)
        self.assertEqual((result["planned"], result["attempts"], result["scored"], result["accepted"]),
                         (18, 2, 1, 0))
        self.assertEqual(result["cost"]["unknown_attempts"], 1)
        self.assertEqual(result["cost"]["known_attempt_subtotal_usd"], 1.25)
        self.assertIsNone(result["cost"]["per_accepted_usd"])
        self.assertEqual(result["decision"], "hold")

    def test_only_complete_held_out_grade_regression_rejects_a_candidate(self):
        pair = next((self.plan[i], self.plan[i + 1]) for i in range(0, 18, 2)
                    if self.plan[i]["task"] != "verified-patch")
        for row in pair:
            baseline = row["arm"] == "baseline"
            grade = "accepted" if baseline else "rejected"
            terminal(self.store, row, {"disposition": grade, "attempt": {
                "cost_usd": None, "scored": True, "accepted": baseline,
                "evidence_complete": True, "grade": grade,
            }})
        self.assertEqual(report(self.store, self.plan, live=True)["decision"], "reject")

    def test_repeated_rejections_on_one_workspace_command_count_once(self):
        def event(sequence, args, message):
            return encode({"type": "item.completed", "item": {
                "id": str(sequence), "type": "mcp_tool_call", "server": "factory", "tool": "workspace",
                "arguments": args, "result": {"content": [{"text": message}]},
            }})
        extra = {"command": "git status", "extra": 1}
        native = (event(1, extra, "invalid workspace command") + event(2, extra, "invalid workspace command")
                  + event(3, {"command": "git status"}, "success") + event(4, extra, "invalid workspace command"))
        self.assertEqual(workspace_rejections(native), 2)

    def test_attempt_and_disposition_cannot_be_overwritten(self):
        row = self.plan[0]
        stopped = {"disposition": "stopped", "attempt": None}
        self.assertEqual(terminal(self.store, row, stopped), terminal(self.store, row, stopped))
        with self.assertRaises(Conflict):
            terminal(self.store, row, {"disposition": "accepted", "attempt": {"job": "other"}})
