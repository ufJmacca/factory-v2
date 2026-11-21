from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory.evidence import Conflict, digest, encode
from factory.fresh_pilot import assignment_rows, require_approval
from factory.harbor_experiment import LIVE_ENVELOPE, plan_digest
from factory.harbor_receipt import workspace_proof_digest


def pilot_design():
    held_out = [{"id": f"task-{i}", "repository": f"owner/project-{i}"} for i in range(8)]
    tasks = ["verified-patch", *(row["id"] for row in held_out)]
    assignments = []
    for position, task in enumerate(tasks):
        first, second = ("candidate", "baseline") if position % 2 else ("baseline", "candidate")
        for arm in (first, second):
            assignments.append({"id": f"p09-{len(assignments) + 1:02d}", "task": task, "arm": arm,
                                "split": "historical" if position == 0 else "held_out"})
    return {
        "id": "p09-fresh-paired-pilot",
        "tasks": {"historical": {"id": "verified-patch"}, "held_out": held_out},
        "schedule": {"assignments": assignments},
        "authorized_envelope": {**LIVE_ENVELOPE, "new_paid_infrastructure_usd": 0},
    }


class FreshPilotTest(unittest.TestCase):
    def test_assignment_ids_and_order_come_from_the_approved_design(self):
        design = pilot_design()
        result = assignment_rows(design)
        self.assertEqual(result, [{**row, "pair": 1} for row in design["schedule"]["assignments"]])
        self.assertTrue(all("pair" not in row for row in design["schedule"]["assignments"]))

    def test_assignments_must_be_unique_paired_and_in_budget(self):
        design = pilot_design()
        rows = design["schedule"]["assignments"]
        for index, changes in ((1, {"id": rows[0]["id"]}), (3, {"task": rows[5]["task"]}),
                               (3, {"arm": rows[2]["arm"]})):
            with self.subTest(changes=changes), self.assertRaises(Conflict):
                changed = deepcopy(design)
                changed["schedule"]["assignments"][index].update(changes)
                assignment_rows(changed)
        design["authorized_envelope"]["max_executions"] += 1
        with self.assertRaises(Conflict):
            assignment_rows(design)

    def test_live_approval_binds_both_the_design_and_execution_lock(self):
        manifest = {"mode": "live", "design_sha256": "a" * 64, "envelope": LIVE_ENVELOPE}
        approval = {"approved_by": "maintainer", "design_sha256": manifest["design_sha256"],
                    "digest": plan_digest(manifest), "envelope": LIVE_ENVELOPE}
        require_approval(manifest, approval)
        for change in ({"design_sha256": "b" * 64}, {"digest": "b" * 64}, {"approved_by": " "},
                       {"envelope": {**LIVE_ENVELOPE, "max_executions": 19}}):
            with self.subTest(change=change), self.assertRaises(Conflict):
                require_approval(manifest, {**approval, **change})

    def test_marker_on_the_right_provider_cannot_cover_a_foreign_command(self):
        scope = {"rollout": "rollout", "attempt": 1, "cwd": "/work/product", "container": "assigned-trial"}
        request = {"run_id": "eval-rollout", "evaluation": {"rollout": "rollout"}}
        provider = {"container": scope["container"]}
        proof = {"rollout": "rollout", "attempt": 1, "marker": "eval-rollout:1"}
        for name, operation in (("command", "workspace-command"), ("write", "marker-write"), ("read", "marker-read")):
            proof[f"{name}_receipt"] = {**scope, "operation": operation, "call_id": f"proof-1-{name}"}
        marker = {"marker": proof["marker"], "assigned_workspace": True, "workspace_proof": digest(encode(proof)),
                  "postflight_receipt": {**scope, "operation": "marker-postflight", "call_id": "postflight"}}
        self.assertEqual(workspace_proof_digest(request, 1, proof, marker, provider), marker["workspace_proof"])

        wrong = {**proof, "command_receipt": {**proof["command_receipt"], "container": "foreign"}}
        with self.assertRaises(Conflict):
            workspace_proof_digest(request, 1, wrong, {**marker, "workspace_proof": digest(encode(wrong))}, provider)
