from collections import Counter
import json
from math import isfinite
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
    "required_scored_coverage": 1.0,
    "scope": "pilot",
    "uncertainty": "Two independent held-out tasks cannot establish promotion generality.",
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
        if (isinstance(args, dict) and "command" in args and len(args) > 1
                and any(row.get("text") == REJECTED for row in text)):
            failures += 1
    return failures


def terminal(store, assignment, result):
    return store.record(assignment["id"], "experiment-terminal", {**assignment, **result})


def totals(rows):
    attempts = [row["attempt"] for row in rows if row["attempt"] is not None]
    billed = [attempt["cost_usd"] for attempt in attempts
              if type(attempt["cost_usd"]) in (float, int) and isfinite(attempt["cost_usd"])
              and attempt["cost_usd"] >= 0]
    accepted = sum(attempt["accepted"] for attempt in attempts)
    return {
        "planned": len(rows), "attempts": len(attempts), "accepted": accepted,
        "scored": sum(attempt["scored"] for attempt in attempts),
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
    complete = all(attempt.get("scored") for attempt in (baseline, candidate))
    rejected_calls = [attempt.get("workspace_rejections") for attempt in (baseline, candidate)]
    return {
        "task": left["task"], "pair": left["pair"],
        "assignments": {arm: row["id"] for arm, row in by_arm.items()},
        "complete": complete,
        "acceptance_regression": bool(complete and baseline["accepted"] and not candidate["accepted"]),
        "candidate_accepted": candidate.get("accepted"),
        "baseline_rejected_calls": rejected_calls[0], "candidate_rejected_calls": rejected_calls[1],
    }


def report(store, plan, *, live=False):
    rows = [store.read(row["id"], "experiment-terminal")
            or {**row, "disposition": "pending", "attempt": None} for row in plan]
    pairs = [pair_result(*rows[i:i + 2]) for i in range(0, len(rows), 2)]
    tuning = [pair for pair in pairs if pair["task"] == TUNING]
    held_out = [pair for pair in pairs if pair["task"] != TUNING]
    historical_delta = None
    if len(tuning) == REPETITIONS and all(
        p["complete"] and p["baseline_rejected_calls"] is not None and p["candidate_rejected_calls"] is not None
        for p in tuning
    ):
        historical_delta = sum(p["baseline_rejected_calls"] - p["candidate_rejected_calls"] for p in tuning)
    prediction = "inconclusive"
    if historical_delta is not None:
        prediction = "supported" if (historical_delta >= THRESHOLDS["historical_min_rejection_reduction"]
                                     and all(p["candidate_accepted"] for p in tuning)) else "disproved"
    summary = totals(rows)
    if not live:
        decision, reason = "hold", "Scripted trials exercise the comparison path. Live quality evidence is pending."
    elif any(pair["acceptance_regression"] for pair in held_out):
        decision, reason = "reject", "A scored held-out pair regressed from acceptance to rejection."
    elif summary["scored"] != len(plan):
        decision, reason = "hold", "Required exact-SHA grading coverage is incomplete."
    else:
        decision, reason = "hold", THRESHOLDS["uncertainty"]
    return {
        **summary, "decision": decision, "reason": reason, "scope": THRESHOLDS["scope"],
        "historical": {"totals": totals([row for row in rows if row["task"] == TUNING]),
                       "pairs": tuning, "rejection_reduction": historical_delta, "prediction": prediction},
        "held_out": {"totals": totals([row for row in rows if row["task"] != TUNING]), "pairs": held_out},
        "assignments": rows,
    }


def plan_digest(manifest):
    return digest(encode(manifest))
