"""Freeze, execute and reconcile one Phase 08 Harbor comparison."""
import asyncio
import json
import os
from pathlib import Path
import sys
from time import time
from urllib.parse import urlencode

from temporalio.client import Client, WorkflowExecutionStatus

from evaluation.harbor.pilot_freeze import pilot_manifest
from evaluation.harbor.prepare import HERE, record_tree
from factory.evidence import Conflict, Evidence, digest, encode, publish
from factory.fresh_pilot import require_approval
from factory.harbor_binding import Binding, TRUSTED_WORKSPACE_MARKER
from factory.harbor_experiment import (
    LIVE_ENVELOPE, ORDER_SEED, REPETITIONS, THRESHOLDS, assignment_plan, plan_digest,
    report, terminal, workspace_rejections,
)
from factory.harbor_receipt import InvalidHarborGrade
from factory.harbor_supervisor import (
    BatchStopped, Harbor, SubmitReplyLost, launch, reconcile, rollout_for, wait_until,
)


RUN = "experiment"
DRAIN = 100


def freeze(store, mode, *, fresh_pilot=False):
    if fresh_pilot:
        manifest = pilot_manifest(store, mode)
        store.record(RUN, "frozen", manifest)
        publish(store.root / "experiment.json", encode(manifest))
        return manifest
    configuration = store.read(RUN, "configuration", required=True)
    tasks = store.read(RUN, "tasks", required=True)
    split_bytes = (HERE / "split.json").read_bytes()
    split = json.loads(split_bytes)
    task_ids = [split["tuning"]["id"], *(row["id"] for row in split["held_out"])]
    if tasks.keys() != set(task_ids) or any(len(tasks[name]["calibration"]) != 3 for name in task_ids[1:]):
        raise Conflict("held-out calibration is incomplete")
    plan = assignment_plan("p08", task_ids, REPETITIONS)
    envelope = LIVE_ENVELOPE if mode == "live" else {
        **LIVE_ENVELOPE, "max_executions": 6, "execution_seconds": 120, "batch_seconds": 1800,
        "subscription_executions": 0,
    }
    first_pairs = {name: next(row["pair"] for row in plan if row["task"] == name) for name in tasks}
    runtime = configuration["arms"]["baseline"]["runtime"]
    manifest = {
        "schema_version": 1, "mode": mode, "split_digest": store.blob(split_bytes), "order_seed": ORDER_SEED,
        "repetitions": REPETITIONS, "paired_model_seed": None, "assignments": plan,
        "harbor_image": (store.root / "harbor-image.txt").read_text().strip(),
        "envelope": envelope, "thresholds": THRESHOLDS, "configuration": configuration, "tasks": tasks,
        "historical_differences": {
            "worker": {"phase05": configuration["source"]["historical_worker_image"], "phase08": runtime["worker_image"]},
            "grader": {"phase05": configuration["source"]["historical_grader"], "phase08": tasks["verified-patch"]["grader_digest"]},
            "historical_grade_scope": "Harbor wrapper around Phase 06 separate verifier",
        },
        "scripted_dispatch": ([row["id"] for row in plan if first_pairs[row["task"]] == row["pair"]]
                              if mode == "scripted" else []),
    }
    store.record(RUN, "frozen", manifest)
    publish(store.root / "experiment.json", encode(manifest))
    return manifest


class GradeDrill(Harbor):
    def __init__(self, token, fault):
        super().__init__(token)
        self.fault = fault

    async def download(self, uri):
        data = await super().download(uri)
        if self.fault and uri.endswith("/verifier/grade.json"):
            grade = json.loads(data)
            grade.update(self.fault)
            return encode(grade)
        return data


def verify_inputs(store, task, arm):
    if digest(encode(record_tree(Path(task["task"])))) != task["task_tree"]:
        raise Conflict("staged task or grader digest changed before dispatch")
    store.read_blob(task["grader_digest"])
    profile = json.loads(store.read_blob(arm["configuration"]))
    for reference in (profile["source_digest"], *profile["skills"].values()):
        store.read_blob(reference)


def verify_pilot_inputs(store, task, arm, checkpoint):
    if not Path(task["task"]).resolve().is_relative_to(store.root.resolve()):
        raise Conflict("fresh pilot task escaped the trusted evidence root")
    verify_inputs(store, task, arm)
    base = json.loads(store.read_blob(checkpoint))
    if base["base_sha"] != task["base_sha"] or base["head_sha"] != task["base_sha"]:
        raise Conflict("fresh pilot base checkpoint differs from the staged task")


async def recover_stopped(api, client, store, name, intent):
    if not intent:
        return
    job = store.read(name, "harbor-job")
    if job is None:
        _, listing = await api.request("/jobs?" + urlencode({"name": name}))
        jobs = [row["id"] for row in listing["items"] if row["name"] == name]
        if len(jobs) > 1 or listing["next_page_token"]:
            raise Conflict("ambiguous stopped assignment in Harbor")
        if not jobs:
            return
        job = store.record(name, "harbor-job", {"job": jobs[0], "request_digest": intent["request_digest"]})
    await rollout_for(api, store, name, job["job"])
    dispatch = store.read(name, "dispatch")
    handle = client.get_workflow_handle("deliver-outcome/" + dispatch["run_id"]) if dispatch else None
    await cancel_attempt(api, store, name, handle)

async def cancel_attempt(api, store, name, handle):
    rollout = store.read(name, "harbor-rollout")
    if rollout:
        binding = Binding(store.root, rollout["rollout"])
        row = await api.rollout(binding.rollout)
        if row["state"] not in ("finished", "cancelled"):
            binding.revoke("experiment_stopped")
            await api.cancel(binding.rollout)
        if handle and (await handle.describe()).status == WorkflowExecutionStatus.RUNNING:
            await handle.cancel()

        async def cleaned():
            row = await api.rollout(binding.rollout)
            temporal_stopped = handle is None or (await handle.describe()).status != WorkflowExecutionStatus.RUNNING
            if temporal_stopped and row["state"] in ("finished", "cancelled") and (
                not store.read(binding.name, "harbor-ready") or store.read(binding.name, "provider-stop")
            ):
                return True

        await wait_until(cleaned, time() + 45, "Harbor attempt cleanup could not be confirmed")


def attempt_record(store, name):
    intent = store.read(name, "harbor-intent")
    if intent is None:
        return None
    job = (store.read(name, "harbor-job") or {}).get("job")
    rollout = (store.read(name, "harbor-rollout") or {}).get("rollout")
    run = Binding(store.root, rollout).name if rollout else None
    dispatch = store.read(name, "dispatch") or {}
    attempt = (store.read(run, "attempt-1") or {}) if run else {}
    export = (store.read(run, "harbor-export") or {}) if run else {}
    grade = (store.read(run, "harbor-grade") or {}) if run else {}
    manifest = (store.read(run, "manifest") or {}) if run else {}
    fixture = store.read(run, "harbor-fixture") if run else None
    observed = store.read(run, "harbor-observed-grade") if run else None
    submitted = export.get("submitted_sha")
    if run and not submitted:
        submitted = (store.read(run, "submission") or {}).get("head_sha")
    native = attempt.get("native_events")
    checked = grade.get("checked_sha")
    scored = bool(checked and checked == submitted)
    accepted = scored and grade["decision"] == "accepted" and manifest.get("status") == "verified" and bool(fixture)
    complete = bool(scored and manifest and native and (grade["decision"] == "rejected" or accepted))
    return {
        "number": 1, "submission_intent": intent["request_digest"], "job": job, "rollout": rollout,
        "run_id": run, "temporal_run_id": dispatch.get("temporal_run_id"),
        "submitted_sha": submitted, "checked_sha": checked, "scored": scored, "accepted": accepted,
        "evidence_complete": complete,
        "grade": grade.get("decision"), "grader_digest": grade.get("grader_digest"),
        "grade_receipt": store.blob(encode(grade)) if grade else None,
        "observed_grade": observed["sha256"] if observed else None,
        "export": store.blob(encode(export)) if export else None,
        "terminal_manifest": store.blob(encode(manifest)) if manifest else None,
        "fixture_receipt": store.blob(encode(fixture)) if fixture else None,
        "evidence": export.get("artifacts", {}), "native_events": native,
        "workspace_rejections": workspace_rejections(store.read_blob(native)) if native else None,
        "usage": attempt.get("usage"), "cost_usd": attempt.get("billing"), "runner_disposition": attempt.get("reason"),
        "execution_reserved": bool(run and store.read(run, "attempt-1-intent")),
        "agent_container": (store.read(run, "harbor-ready") or {}).get("container") if run else None,
        "workflow_disposition": manifest.get("reason"),
    }


async def drive(store, manifest, action):
    live = manifest["mode"] == "live"
    fresh_pilot = manifest["schema_version"] == 2
    envelope = manifest["envelope"]
    frozen = plan_digest(manifest)
    if live:
        approval = store.read(RUN, "approval", required=True)
        if fresh_pilot:
            require_approval(manifest, approval)
        elif approval["digest"] != frozen or approval["envelope"] != envelope or not approval["approved_by"].strip():
            raise Conflict("live approval must bind the frozen experiment and envelope")
    clock = store.read(RUN, "clock")
    if clock is None:
        if live and fresh_pilot:
            configuration = manifest["configuration"]
            for task_id, task in manifest["tasks"].items():
                for arm in configuration["arms"].values():
                    verify_pilot_inputs(store, task, arm, configuration["base_checkpoints"][task_id])
        clock = store.record(RUN, "clock", {"deadline": time() + envelope["batch_seconds"]})
    deadline = clock["deadline"]
    client = await Client.connect(os.environ["TEMPORAL_ADDRESS"])
    token = os.environ["BRIDGE_API_KEY"]
    plan = manifest["assignments"]
    scripted = manifest["scripted_dispatch"]
    exhausted = None
    stop_file = store.root / "stop"
    api = Harbor(token)
    for row in plan:
        name, task_id, arm_name = (row[key] for key in ("id", "task", "arm"))
        prior = store.read(name, "experiment-terminal")
        if prior:
            if (prior.get("attempt") or {}).get("runner_disposition") == "quota_exhausted":
                exhausted = "subscription_quota"
            if fresh_pilot and prior.get("batch_stop"):
                exhausted = prior["batch_stop"]
            continue
        started = store.read(name, "harbor-intent")
        stop = exhausted or ("deadline" if time() + DRAIN >= deadline else None)
        if stop_file.exists():
            stop = "operator_stop"
        if not live and name not in scripted:
            stop = "scripted_batch_cap"
        if stop:
            await recover_stopped(api, client, store, name, started)
            terminal(store, row, {"disposition": "stopped", "reason": stop, "attempt": attempt_record(store, name)})
            continue
        task = manifest["tasks"][task_id]
        config = manifest["configuration"]
        arm = config["arms"][arm_name]
        common = {
            **arm, "schema_version": 1, "max_attempts": 1,
            "workspace_marker": TRUSTED_WORKSPACE_MARKER if fresh_pilot else True,
            "base_sha": task["base_sha"], "base_checkpoint": config["base_checkpoints"][task_id],
            "transport": config["transport"],
        }
        fault = None
        if not live and not fresh_pilot and arm_name == "candidate":
            fault = {"checks": {"unexpected": True}} if task_id == "verified-patch" else (
                {"grader_digest": "0" * 64} if task_id == "profile-preview" else None)
        trial_api = GradeDrill(token, fault) if fault else api
        lock = {**task, "task_id": task_id}
        if not live and task_id == "verified-patch" and arm_name == "baseline":
            lock["scripted_rejection"] = True
        handle = None
        disposition = None
        reason = None
        if fresh_pilot:
            try:
                verify_pilot_inputs(store, task, arm, common["base_checkpoint"])
            except (Conflict, FileNotFoundError, KeyError) as error:
                exhausted = "frozen_input_drift"
                await recover_stopped(api, client, store, name, started)
                terminal(store, row, {"disposition": "failed", "reason": str(error), "batch_stop": exhausted,
                                      "attempt": attempt_record(store, name)})
                continue
        try:
            if not fresh_pilot:
                verify_inputs(store, task, arm)
            request, handle = await launch(
                trial_api, client, store, name, frozen=lock, common=common,
                minutes=envelope["execution_seconds"] / 60, deadline_limit=deadline,
                lose_reply=action == "lose-reply",
            )
            if stop_file.exists():
                raise BatchStopped
            store.record(name, "runner-allowed", {"allowed": True})
            result = await reconcile(trial_api, store, request, handle, notify=live, stop=stop_file)
            reason = result.get("reason", result.get("state"))
        except SubmitReplyLost:
            if action != "lose-reply":
                raise
            return
        except BatchStopped:
            await cancel_attempt(api, store, name, handle)
            disposition, reason = "stopped", stop or "operator_stop"
        except InvalidHarborGrade as error:
            store.record(request["run_id"], "verify", {"reason": "harbor_invalid_grade"})
            await asyncio.wait_for(handle.result(), timeout=20)
            disposition, reason = "invalid_grade", str(error)
            if fresh_pilot:
                exhausted = "invalid_protected_grade"
        except (Conflict, KeyError, TimeoutError) as error:
            intent = store.read(name, "harbor-intent")
            if intent and not store.read(name, "harbor-rollout"):
                raise
            await cancel_attempt(api, store, name, handle)
            disposition, reason = "failed", str(error)
        attempt = attempt_record(store, name)
        if attempt and attempt["runner_disposition"] == "quota_exhausted":
            exhausted = "subscription_quota"
        if fresh_pilot and attempt and attempt["runner_disposition"] in {
            "pinned_configuration_unavailable", "runner_infrastructure_failed", "workspace_marker_missing",
        }:
            exhausted = "trusted_workspace_proof_failed"
        if disposition is None:
            disposition = ("accepted" if attempt and attempt["accepted"] else
                           "rejected" if attempt and attempt["scored"] and attempt["grade"] == "rejected"
                           else "incomplete")
        result = {"disposition": disposition, "reason": reason, "attempt": attempt}
        if fresh_pilot and exhausted:
            result["batch_stop"] = exhausted
        terminal(store, row, result)

    result = report(store, plan, live=live, fresh_pilot=fresh_pilot)
    if result["dispositions"].get("pending", 0):
        raise Conflict("experiment stopped with unreconciled assignments")
    if not live and not stop_file.exists() and fresh_pilot:
        if result["dispositions"] != {"accepted": 2, "stopped": 16}:
            raise Conflict(f"fresh scripted pilot disagrees with expectations: {result['dispositions']}")
    if not live and not stop_file.exists() and not fresh_pilot:
        duplicate = store.read(scripted[0], "harbor-duplicate", required=True)
        recovered = store.read(scripted[0], "harbor-job", required=True)
        faults = {row["task"]: row["reason"] for row in result["assignments"] if row["disposition"] == "invalid_grade"}
        if (duplicate["job"] != recovered["job"] or result["attempts"] != len(scripted)
                or faults != {"verified-patch": "Harbor protected grade has invalid check results",
                              "profile-preview": "Harbor grade provenance differs from the locked submission"}
                or result["dispositions"].get("invalid_grade") != 2
                or result["dispositions"].get("stopped") != len(plan) - len(scripted)
                or result["accepted"] != len(scripted) - 2):
            raise Conflict(f"scripted drill disagrees with expectations: {result['dispositions']}")
    attempts = [row["attempt"] for row in result["assignments"] if row["attempt"]]
    result["resources"] = {
        "harbor_jobs": len({row["job"] for row in attempts if row["job"]}),
        "agent_containers": sum(bool(row["agent_container"]) for row in attempts),
        "verifier_grades_observed": sum(bool(row["observed_grade"]) for row in attempts),
        "batch_seconds_used": round(time() - (deadline - envelope["batch_seconds"]), 2),
        "calibration_trials": sum(len(task.get("calibration", [])) for task in manifest["tasks"].values()),
        "execution_reservations": sum(row["execution_reserved"] for row in attempts),
        "subscription_execution_upper_bound": sum(row["execution_reserved"] for row in attempts) if live else 0,
        "fixture_effects": sum(bool(row["fixture_receipt"]) for row in attempts),
        "production_effects": 0,
    }
    publish(store.root / "summary.json", encode({"experiment_digest": frozen, **result}))


async def main():
    os.umask(0o077)
    store = Evidence(Path(os.environ["BRIDGE_ROOT"]))
    action, mode, *variant = sys.argv[1:]
    if variant and variant != ["fresh-pilot"]:
        raise Conflict("invalid comparison variant")
    fresh_pilot = bool(variant)
    if action == "freeze":
        manifest = freeze(store, mode, fresh_pilot=fresh_pilot)
        if mode == "live" and not fresh_pilot:
            store.record(RUN, "approval", {
                "approved_by": os.environ["PHASE08_APPROVED_BY"], "digest": plan_digest(manifest),
                "envelope": manifest["envelope"],
            })
    else:
        manifest = store.read(RUN, "frozen", required=True)
        if (manifest["schema_version"] != (2 if fresh_pilot else 1) or mode != manifest["mode"]
                or action not in ("run", "lose-reply") or (action == "lose-reply" and mode == "live")):
            raise Conflict("invalid comparison mode or action")
        with store.lock(RUN):
            await drive(store, manifest, action)


if __name__ == "__main__":
    asyncio.run(main())
