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

    def fresh_pilot(self, *, mechanism_pairs=2, historical_pass=True, historical_complete=True,
                    acceptance_gain=True, acceptance_loss=False):
        plan = []
        for pair, task in enumerate(["verified-patch", *(f"upstream-{i}" for i in range(1, 9))]):
            for arm in ("baseline", "candidate"):
                row = {"id": f"p09-{len(plan) + 1:02d}", "task": task, "pair": 1, "arm": arm}
                plan.append(row)
                if pair == 0 and arm == "candidate" and not historical_complete:
                    continue
                passed = not ((pair == 0 and not historical_pass)
                              or (pair == 1 and arm == "baseline" and acceptance_gain)
                              or (pair == 3 and arm == "candidate" and acceptance_loss))
                grade = "accepted" if passed else "rejected"
                terminal(self.store, row, {"disposition": grade, "attempt": {
                    "cost_usd": None, "scored": True, "accepted": passed, "evidence_complete": True, "grade": grade,
                    "workspace_rejections": 2 if arm == "baseline" and 0 < pair <= mechanism_pairs else 0,
                }})
        return plan

    def test_fresh_pilot_reports_scoped_review_eligibility(self):
        plan = self.fresh_pilot()
        result = report(self.store, plan, live=True, fresh_pilot=True)
        self.assertEqual(result["decision"], "eligible_for_review")
        self.assertEqual(result["historical"]["totals"]["accepted"], 2)
        self.assertEqual(result["held_out"]["acceptance_gain"], 1)
        self.assertEqual(result["held_out"]["mechanism"]["pairs_with_fewer_rejections"], 2)
        self.assertEqual(result["held_out"]["acceptance_pairs"], {
            "candidate_only": 1, "baseline_only": 0, "both_accepted": 7, "both_rejected": 0,
            "one_sided_sign_probability": 0.5,
        })
        self.assertEqual(report(self.store, plan, fresh_pilot=True)["decision"], "hold")

    def test_fresh_pilot_holds_when_one_task_accounts_for_the_reduction(self):
        plan = self.fresh_pilot(mechanism_pairs=1)
        result = report(self.store, plan, live=True, fresh_pilot=True)
        self.assertEqual(result["decision"], "hold")
        self.assertEqual(result["held_out"]["mechanism"]["rejection_reduction"], 2)

    def test_fresh_pilot_holds_when_the_historical_control_fails(self):
        plan = self.fresh_pilot(historical_pass=False)
        self.assertEqual(report(self.store, plan, live=True, fresh_pilot=True)["decision"], "hold")

    def test_fresh_pilot_holds_on_a_code_tie(self):
        plan = self.fresh_pilot(acceptance_gain=False)
        self.assertEqual(report(self.store, plan, live=True, fresh_pilot=True)["decision"], "hold")

    def test_fresh_pilot_holds_when_required_data_is_missing(self):
        plan = self.fresh_pilot(historical_complete=False)
        result = report(self.store, plan, live=True, fresh_pilot=True)
        self.assertEqual((result["decision"], result["scored"], result["planned"]), ("hold", 17, 18))

    def test_fresh_pilot_rejects_a_scored_regression_even_if_another_pair_is_incomplete(self):
        plan = self.fresh_pilot(historical_complete=False, acceptance_loss=True)
        self.assertEqual(report(self.store, plan, live=True, fresh_pilot=True)["decision"], "reject")
