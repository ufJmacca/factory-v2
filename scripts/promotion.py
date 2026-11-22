"""Trusted admission, activation, and rollback for one Factory deployment."""

import argparse
import json
from math import isfinite
import os
from pathlib import Path
import re
from time import time

from factory.codex_bridge import tool_definitions
from factory.evidence import Conflict, Evidence, digest, encode


REGISTRY = "factory-promotion"
METRICS = ("reopens", "rollbacks", "reviewer_seconds", "cost_microusd")
ADMISSION_FIELDS = {"factory_sha", "configuration", "runtime", "workspace_image", "red_argv_guidance"}


def require(condition, message):
    if not condition:
        raise Conflict(message)


def identity(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value) is not None


def named(value):
    return isinstance(value, str) and bool(value.strip())


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def revision(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def deadline(value):
    return type(value) in (int, float) and isfinite(value)


def metrics_valid(metrics, *, allow_unknown=False):
    return isinstance(metrics, dict) and set(metrics) == set(METRICS) and all(
        integer(value) or (allow_unknown and value is None and key in {"reviewer_seconds", "cost_microusd"})
        for key, value in metrics.items()
    )


class Promotion:
    def __init__(self, store: Evidence):
        self.store = store

    def _blob(self, reference):
        return json.loads(self.store.read_blob(reference))

    def version(self, reference):
        version = self._blob(reference)
        guidance = version.get("red_argv_guidance")
        require(version.keys() == ADMISSION_FIELDS | {"schema_version", "source_inputs_sha256", "tool_definition_sha256"}
                and version["schema_version"] == 1 and type(guidance) is bool
                and version["tool_definition_sha256"] == digest(encode(tool_definitions(red_argv_guidance=guidance))),
                "invalid or unavailable factory version")
        self.store.read_blob(version["configuration"])
        return version

    def initialize(self, baseline):
        with self.store.lock(REGISTRY):
            require(self.version(baseline)["red_argv_guidance"] is False, "the baseline must use baseline guidance")
            return self.store.record(REGISTRY, "baseline", {"generation": 0, "digest": baseline})

    def _history(self):
        return [json.loads(path.read_bytes())
                for path in sorted(self.store.run_dir(REGISTRY).glob("transition-*.json"))]

    def _baseline(self):
        return self.store.read(REGISTRY, "baseline", required=True)

    def _pointer(self, history):
        return history[-1]["active"] if history else self._baseline()

    def active(self):
        with self.store.lock(REGISTRY):
            return self._pointer(self._history())

    def _activate(self, approval, baseline):
        require(approval["target"] != baseline and approval["expected"]["digest"] == baseline,
                "restore the baseline before activating another candidate")
        scope = approval.get("scope")
        require(isinstance(scope, dict)
                and scope.keys() == {"outcome_class", "cohort", "max_runs", "observation_seconds", "limits"}
                and identity(scope["outcome_class"]) and identity(scope["cohort"])
                and integer(scope["max_runs"], 1) and integer(scope["observation_seconds"], 1)
                and metrics_valid(scope["limits"]), "activation requires an explicit bounded cohort")
        target = approval["target"]
        candidate = self.version(target)
        build = candidate["runtime"]["workflow_build"]
        review = self._blob(approval["review"])
        require(review.get("decision") == "promote" and named(review.get("reviewer"))
                and review.get("tested_sha") == build and review.get("baseline") == baseline
                and review.get("candidate") == target, "candidate lacks the matching promotion decision")
        self.store.read_blob(review["experiment_sha256"])
        self.store.read_blob(review["evidence_sha256"])
        parity = self._blob(approval["parity"])
        require(parity.get("schema_version") == 1 and revision(parity.get("merged_sha"))
                and parity.get("candidate") == target and parity.get("tested_sha") == build
                and parity.get("source_inputs_sha256") == candidate["source_inputs_sha256"]
                and parity.get("worker_image") == candidate["runtime"]["worker_image"],
                "merged effective inputs lack matching parity evidence")
        initial = self.version(baseline)
        require(candidate == {**initial, "red_argv_guidance": True,
                              "tool_definition_sha256": candidate["tool_definition_sha256"]},
                "candidate differs from the reviewed baseline outside red argv guidance")

    def transition(self, approval, *, now=None):
        require(isinstance(approval, dict) and identity(approval.get("id")), "invalid transition ID")
        with self.store.lock(REGISTRY):
            history = self._history()
            reference = digest(encode(approval))
            for transition in history:
                if transition["id"] == approval["id"]:
                    require(transition["approval_sha256"] == reference, "transition ID already used")
                    return transition

            action = approval.get("action")
            fields = {"id", "action", "expected", "target", "approved_by", "expires_at"}
            fields |= {"review", "parity", "scope"} if action == "activate" else {"reason"}
            require(action in {"activate", "rollback"} and approval.keys() == fields,
                    "invalid transition authorization")
            now = time() if now is None else now
            expires = approval["expires_at"]
            require(named(approval["approved_by"]) and deadline(expires) and now < expires,
                    "transition authorization is expired or unnamed")
            current = self._pointer(history)
            expected = approval["expected"]
            require(isinstance(expected, dict) and integer(expected.get("generation")) and expected == current,
                    "expected active version changed")
            baseline = self._baseline()["digest"]
            target = approval["target"]
            if action == "activate":
                self._activate(approval, baseline)
            else:
                require(current["digest"] != baseline and target == baseline and named(approval["reason"]),
                        "rollback must restore the approved baseline and record a reason")
            next_active = {"generation": current["generation"] + 1, "digest": target}
            return self.store.record(REGISTRY, f"transition-{next_active['generation']:020d}", {
                "id": approval["id"], "action": action, "previous": current, "active": next_active,
                "approval": approval, "approval_sha256": reference, "applied_at": now,
                "rollback_target": baseline,
            })

    def _report(self, transition, new_event=None):
        scope = transition["approval"]["scope"]
        generation = transition["active"]["generation"]
        requests = [path.parent for path in (self.store.root / "runs").glob("*/request.json")
                    if json.loads(path.read_bytes()).get("factory_cohort") == generation]
        totals = dict.fromkeys(METRICS, 0)
        unknown = dict.fromkeys(METRICS, 0)
        observed = 0
        for run in requests:
            events = [json.loads(path.read_bytes())["event"] for path in run.glob("observation-*.json")]
            if new_event and run.name == new_event["run_id"]:
                events.append(new_event)
            observed += bool(events)
            for event in events:
                for metric, amount in event["metrics"].items():
                    if amount is None:
                        unknown[metric] += 1
                    else:
                        totals[metric] += amount
        exceeded = [metric for metric in METRICS if totals[metric] > scope["limits"][metric]]
        decision = "within_limits" if observed and observed == len(requests) and not any(unknown.values()) else "incomplete"
        return {
            "generation": generation, "cohort": scope["cohort"], "admitted": len(requests),
            "observed": observed, "totals": totals, "unknown": unknown, "exceeded": exceeded,
            "decision": "rollback_required" if exceeded else decision,
        }

    def report(self, generation):
        with self.store.lock(REGISTRY):
            return self._report(self._cohort(generation))

    def _cohort(self, generation):
        require(integer(generation, 1), "no such activation cohort")
        transition = self.store.read(REGISTRY, f"transition-{generation:020d}")
        require(transition is not None and transition["action"] == "activate", "no such activation cohort")
        return transition

    def admit(self, approved, *, now=None):
        reserved = ADMISSION_FIELDS | {"worker_image", "transport", "factory_digest", "factory_cohort",
                                      "admission_sha256", "admitted_at", "evaluation"}
        require(isinstance(approved, dict) and not reserved.intersection(approved)
                and identity(approved.get("run_id")) and identity(approved.get("outcome_class"))
                and named(approved.get("approved_by")) and named(approved.get("outcome"))
                and type(approved.get("schema_version")) is int and approved["schema_version"] == 1,
                "admit an approved outcome without runtime overrides")
        run_id = approved["run_id"]
        require(run_id != REGISTRY, "run ID is reserved for promotion")
        reference = digest(encode(approved))
        with self.store.lock(REGISTRY):
            if existing := self.store.read(run_id, "request"):
                require(existing.get("admission_sha256") == reference, "admitted outcome changed")
                return existing
            now = time() if now is None else now
            baseline = self._baseline()["digest"]
            selected, generation = baseline, None
            history = self._history()
            if history and history[-1]["action"] == "activate":
                activation = history[-1]
                scope = activation["approval"]["scope"]
                if (approved["outcome_class"] == scope["outcome_class"]
                        and activation["applied_at"] <= now < activation["applied_at"] + scope["observation_seconds"]):
                    report = self._report(activation)
                    if report["admitted"] < scope["max_runs"] and not report["exceeded"]:
                        selected = activation["active"]["digest"]
                        generation = activation["active"]["generation"]
            version = self.version(selected)
            profile = self._blob(version["configuration"])
            attempts = approved.get("max_attempts")
            expires = approved.get("deadline_epoch")
            base = approved.get("base_sha")
            require(revision(base) and integer(attempts, 1) and attempts <= profile["profile"]["max_attempts"]
                    and deadline(expires) and now < expires, "invalid delivery base, attempt limit, or deadline")
            checkpoint = self._blob(approved["base_checkpoint"])
            require(checkpoint.get("base_sha") == checkpoint.get("head_sha") == base,
                    "delivery checkpoint does not start at the approved base")
            self.store.read_blob(approved["grader_digest"])
            runtime = version["runtime"]
            return self.store.admit({
                **approved, **{key: version[key] for key in ADMISSION_FIELDS},
                "worker_image": runtime["worker_image"], "transport": runtime["transport"],
                "factory_digest": selected, "factory_cohort": generation, "admission_sha256": reference,
                "admitted_at": now,
            })

    def observe(self, event, *, now=None):
        require(isinstance(event, dict) and event.keys() == {"id", "run_id", "observer", "metrics"}
                and identity(event["id"]) and identity(event["run_id"]) and named(event["observer"])
                and metrics_valid(event["metrics"], allow_unknown=True), "invalid cohort observation")
        run_id, key = event["run_id"], "observation-" + event["id"]
        with self.store.lock(REGISTRY):
            if existing := self.store.read(run_id, key):
                require(existing["event"] == event, "observation ID already used")
                return existing
            now = time() if now is None else now
            request = self.store.read(run_id, "request", required=True)
            transition = self._cohort(request.get("factory_cohort"))
            close = transition["applied_at"] + transition["approval"]["scope"]["observation_seconds"]
            require(request["admitted_at"] <= now < close, "observation window is closed")
            return self.store.record(run_id, key, {
                "event": event, "observed_at": now, "report": self._report(transition, event),
            })


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("initialize").add_argument("baseline")
    actions.add_parser("active")
    actions.add_parser("report").add_argument("generation", type=int)
    for action in ("transition", "admit", "observe"):
        actions.add_parser(action).add_argument("record", type=Path)
    args = parser.parse_args()
    registry = Promotion(Evidence(args.evidence.resolve()))
    method = getattr(registry, args.action)
    if args.action == "active":
        result = method()
    elif args.action in {"initialize", "report"}:
        result = method(args.baseline if args.action == "initialize" else args.generation)
    else:
        result = method(json.loads(args.record.read_bytes()))
    print(encode(result).decode(), end="")


if __name__ == "__main__":
    main()
