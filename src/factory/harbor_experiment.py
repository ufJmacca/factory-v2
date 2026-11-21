from collections import Counter
import json
from math import comb, isfinite
from random import Random

from factory.evidence import Conflict, digest, encode, validate_run_id
from factory.proposal_evidence import REJECTED


ARMS = ("baseline", "candidate")
TUNING = "verified-patch"
ORDER_SEED = 8026
REPETITIONS = 3
LIVE_ENVELOPE = {
    "max_assignments": 18, "max_executions": 18, "concurrency": 1, "retries": 0,
    "execution_seconds": 600, "batch_seconds": 14400, "api_spend_usd": 0,
    "agent_cpus": 1, "agent_memory_mb": 512, "verifier_cpus": 1, "verifier_memory_mb": 768,
}
THRESHOLDS = {
    "historical_min_rejection_reduction": 1,
    "historical_candidate_all_pass": True,
    "held_out_max_acceptance_regressions": 0,
    "held_out_min_acceptance_gain": 1,
    "required_scored_coverage": 1.0,
    "scope": "pilot",
    "uncertainty": "Two independent held-out tasks cannot establish promotion generality.",
}
FRESH_PILOT_THRESHOLDS = {
    "historical_assignments_accepted": 2,
    "held_out_max_acceptance_regressions": 0,
    "held_out_min_acceptance_gain": 1,
    "held_out_min_rejection_reduction": 2,
    "held_out_min_pairs_with_fewer_rejections": 2,
    "required_scored_coverage": 1.0,
}


def assignment_plan(batch, tasks, repetitions):
    pairs = [(task, pair) for task in tasks for pair in range(1, repetitions + 1)]
    rng = Random(ORDER_SEED)
    rng.shuffle(pairs)
    plan = []
    for task, pair in pairs:
        arms = list(ARMS)
        rng.shuffle(arms)
        for arm in arms:
            name = validate_run_id(f"{batch}-{len(plan) + 1:02}")
            plan.append({"id": name, "task": task, "pair": pair, "arm": arm})
    return plan


def compare_profiles(baseline, candidate, path, before, after):
    required = (baseline["skills"].get(path), candidate["skills"].get(path))
    if required != (before, after) or before == after:
        raise Conflict("resolved delivery skill differs from the proposal")
    expected = {**baseline, "factory_sha": candidate["factory_sha"],
                "skills": {**baseline["skills"], path: after}}
    if candidate != expected:
        raise Conflict("resolved input drift beyond the declared skill")


def workspace_rejections(native):
    completed = set()
    failures = 0
    rejected_command = None
    for line in native.splitlines():
        event = json.loads(line)
        if event["type"] != "item.completed":
            continue
        item = event["item"]
        if (item.get("type"), item.get("server"), item.get("tool")) != ("mcp_tool_call", "factory", "workspace"):
            continue
        if item["id"] in completed:
            continue
        completed.add(item["id"])
        args = item.get("arguments")
        text = (item.get("result") or {}).get("content", [])
        command = args.get("command") if isinstance(args, dict) else None
        if isinstance(command, str) and len(args) > 1 and any(row.get("text") == REJECTED for row in text):
            failures += command != rejected_command
            rejected_command = command
        else:
            rejected_command = None
    return failures


def terminal(store, assignment, result):
    return store.record(assignment["id"], "experiment-terminal", {**result, **assignment})


def totals(rows):
    attempts = [row["attempt"] for row in rows if row["attempt"] is not None]
    billed = [attempt["cost_usd"] for attempt in attempts
              if type(attempt["cost_usd"]) in (float, int) and isfinite(attempt["cost_usd"])
              and attempt["cost_usd"] >= 0]
    accepted = sum(attempt["accepted"] for attempt in attempts)
    return {
        "planned": len(rows), "attempts": len(attempts), "accepted": accepted,
        "scored": sum(attempt["scored"] for attempt in attempts),
        "evidence_complete": sum(attempt.get("evidence_complete", False) for attempt in attempts),
        "dispositions": dict(Counter(row["disposition"] for row in rows)),
        "cost": {
            "known_attempt_subtotal_usd": sum(billed) if billed else None,
            "known_attempts": len(billed), "unknown_attempts": len(attempts) - len(billed),
            "infrastructure_usd": None, "per_accepted_usd": None,
        },
    }


def pair_result(left, right):
    by_arm = {row["arm"]: row for row in (left, right)}
    baseline, candidate = (by_arm[arm]["attempt"] or {} for arm in ARMS)
    complete = all(attempt.get("evidence_complete") for attempt in (baseline, candidate))
    rejected_calls = [attempt.get("workspace_rejections") for attempt in (baseline, candidate)]
    return {
        "task": left["task"], "pair": left["pair"],
        "assignments": {arm: row["id"] for arm, row in by_arm.items()},
        "complete": complete,
        "acceptance_regression": bool(complete and baseline["accepted"] and candidate.get("grade") == "rejected"),
        "baseline_accepted": baseline.get("accepted"), "candidate_accepted": candidate.get("accepted"),
        "baseline_rejected_calls": rejected_calls[0], "candidate_rejected_calls": rejected_calls[1],
    }


def paired_effects(pairs):
    complete = [pair for pair in pairs if pair["complete"]]
    outcomes = Counter((pair["baseline_accepted"], pair["candidate_accepted"]) for pair in complete)
    wins, losses = outcomes[False, True], outcomes[True, False]
    discordances = wins + losses
    sign_probability = sum(comb(discordances, count) for count in range(wins, discordances + 1)) / 2**discordances
    differences = [pair["baseline_rejected_calls"] - pair["candidate_rejected_calls"] for pair in complete
                   if pair["baseline_rejected_calls"] is not None and pair["candidate_rejected_calls"] is not None]
    return {
        "acceptance_pairs": {
            "candidate_only": wins, "baseline_only": losses, "both_accepted": outcomes[True, True],
            "both_rejected": outcomes[False, False], "one_sided_sign_probability": sign_probability,
        },
        "mechanism": {
            "pairs_measured": len(differences), "pairs_with_fewer_rejections": sum(delta > 0 for delta in differences),
            "rejection_reduction": sum(differences) if len(differences) == len(pairs) else None,
        },
    }


def fresh_pilot_decision(historical, mechanism):
    if len(historical) != 1 or not all(historical[0][key] for key in ("baseline_accepted", "candidate_accepted")):
        return "hold", "Both historical control assignments must pass."
    reduction = mechanism["rejection_reduction"]
    if (reduction is None or reduction < FRESH_PILOT_THRESHOLDS["held_out_min_rejection_reduction"]
            or mechanism["pairs_with_fewer_rejections"] < FRESH_PILOT_THRESHOLDS["held_out_min_pairs_with_fewer_rejections"]):
        return "hold", "The reduction in extra-argument rejections did not meet the held-out rule."
    return "eligible_for_review", "The pilot met its quality and workspace-rejection gates for scoped review."


def report(store, plan, *, live=False, fresh_pilot=False):
    rows = [store.read(row["id"], "experiment-terminal")
            or {**row, "disposition": "pending", "attempt": None} for row in plan]
    pairs = [pair_result(*rows[i:i + 2]) for i in range(0, len(rows), 2)]
    tuning = [pair for pair in pairs if pair["task"] == TUNING]
    held_out = [pair for pair in pairs if pair["task"] != TUNING]
    historical_delta = None
    if len(tuning) == (1 if fresh_pilot else REPETITIONS) and all(
        p["complete"] and p["baseline_rejected_calls"] is not None and p["candidate_rejected_calls"] is not None
        for p in tuning
    ):
        historical_delta = sum(p["baseline_rejected_calls"] - p["candidate_rejected_calls"] for p in tuning)
    held_out_gain = None
    if held_out and all(pair["complete"] for pair in held_out):
        held_out_gain = sum(pair["candidate_accepted"] - pair["baseline_accepted"] for pair in held_out)
    prediction = "inconclusive"
    if fresh_pilot:
        prediction = "historical_control"
    elif historical_delta is not None:
        prediction = "supported" if (historical_delta >= THRESHOLDS["historical_min_rejection_reduction"]
                                     and all(p["candidate_accepted"] for p in tuning)) else "disproved"
    summary = totals(rows)
    effects = paired_effects(held_out) if fresh_pilot else {}
    thresholds = FRESH_PILOT_THRESHOLDS if fresh_pilot else THRESHOLDS
    if not live:
        decision, reason = "hold", "Scripted trials exercise the comparison path. Live quality evidence is pending."
    elif any(pair["acceptance_regression"] for pair in held_out):
        decision, reason = "reject", "A scored held-out pair regressed from acceptance to rejection."
    elif summary["evidence_complete"] != len(plan):
        decision, reason = "hold", "Required exact-SHA grading coverage is incomplete."
    elif held_out_gain is None or held_out_gain < thresholds["held_out_min_acceptance_gain"]:
        decision, reason = "hold", "The minimum held-out acceptance gain was not demonstrated."
    elif fresh_pilot:
        decision, reason = fresh_pilot_decision(tuning, effects["mechanism"])
    else:
        decision, reason = "hold", THRESHOLDS["uncertainty"]
    return {
        **summary, "decision": decision, "reason": reason,
        "scope": "scoped-pilot-review" if fresh_pilot else THRESHOLDS["scope"],
        "historical": {"totals": totals([row for row in rows if row["task"] == TUNING]),
                       "pairs": tuning, "rejection_reduction": historical_delta, "prediction": prediction},
        "held_out": {"totals": totals([row for row in rows if row["task"] != TUNING]),
                     "pairs": held_out, "acceptance_gain": held_out_gain, **effects},
        "assignments": rows,
    }


def plan_digest(manifest):
    return digest(encode(manifest))
