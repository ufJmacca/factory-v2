import json
import re

from factory.checkpoint import decode
from factory.evidence import Conflict, digest, encode
from factory.fixture import Ledger, validate_receipt
from factory.harbor_binding import TRUSTED_WORKSPACE_MARKER, WORKSPACE_CWD


ISOLATION_CHECKS = (
    "root_read_only", "tests_read_only", "agent_workspace_absent", "agent_marker_absent",
    "docker_socket_absent", "loopback_only",
)
GRADE_CHECKS = {"verified", "unknown_run", "missing_artifact", "corrupt"}



class InvalidHarborGrade(Conflict):
    pass


def runtime_pin(profile, build, worker, transport):
    return {
        "workflow": profile["profile"]["workflow"], "workflow_build": build, "worker_image": worker,
        "cli_build": profile["cli_build"], "model": profile["model"],
        "reasoning_effort": profile["reasoning_effort"], "skills": profile["skills"],
        "profile_digest": profile["profile_digest"], "transport": transport,
        "api_fallback": profile["api_fallback"],
    }


def trajectory(native, run_id, profile, usage):
    steps = []
    for line in native.splitlines():
        event = json.loads(line)
        item = event.get("item", {})
        if event["type"] != "item.completed":
            continue
        step = {"step_id": len(steps) + 1, "source": "agent", "model_name": profile["model"],
                "message": item.get("text") or item.get("type", "Codex event"), "extra": {"native_item": item}}
        if item.get("type") == "mcp_tool_call":
            call_id = item["id"]
            step["tool_calls"] = [{
                "tool_call_id": call_id, "function_name": item["tool"],
                "arguments": item.get("arguments", {}),
            }]
            step["observation"] = {"results": [{
                "source_call_id": call_id,
                "content": encode(item.get("result") or item.get("error")).decode().strip(),
            }]}
        steps.append(step)
    if not steps:
        raise Conflict("Codex trace contains no completed items")
    usage = usage or {}
    return {
        "schema_version": "ATIF-v1.8", "session_id": run_id,
        "agent": {"name": "codex-cli-chatgpt", "version": profile["cli_build"], "model_name": profile["model"]},
        "steps": steps,
        "final_metrics": {"total_steps": len(steps), "total_prompt_tokens": usage.get("input_tokens"),
                          "total_completion_tokens": usage.get("output_tokens"),
                          "total_cached_tokens": usage.get("cached_input_tokens")},
    }


def workspace_proof_digest(request, attempt, proof, marker, provider):
    rollout = request["evaluation"]["rollout"]
    proof_digest = digest(encode(proof))
    expected_marker = f"{request['run_id']}:{attempt}"
    if (proof.get("rollout") != rollout or proof.get("attempt") != attempt
            or proof.get("marker") != expected_marker or marker.get("marker") != expected_marker
            or marker.get("assigned_workspace") is not True or marker.get("workspace_proof") != proof_digest):
        raise Conflict("Harbor export requires the assigned workspace proof")
    scope = {"rollout": rollout, "attempt": attempt, "cwd": WORKSPACE_CWD, "container": provider["container"]}
    for suffix, operation in (("command", "workspace-command"), ("write", "marker-write"), ("read", "marker-read")):
        if proof.get(f"{suffix}_receipt") != {**scope, "operation": operation, "call_id": f"proof-{attempt}-{suffix}"}:
            raise Conflict("Harbor workspace proof does not match the provider command and marker receipts")
    final_read = marker.get("postflight_receipt", {})
    if (not isinstance(final_read.get("call_id"), str) or not final_read["call_id"]
            or final_read != {**scope, "operation": "marker-postflight", "call_id": final_read["call_id"]}):
        raise Conflict("Harbor workspace proof does not match the provider postflight receipt")
    return proof_digest


def export_submission(store, request, workspace, attempt):
    run_id = request["run_id"]
    proof = {}
    if request.get("workspace_marker") == TRUSTED_WORKSPACE_MARKER:
        number = attempt["attempt"]
        key = f"attempt-{number}"
        proof = {"workspace_proof": workspace_proof_digest(
            request, number, store.read(run_id, f"{key}-workspace-proof", required=True),
            store.read(run_id, f"{key}-marker", required=True), store.read(run_id, "provider-start", required=True),
        ), "workspace_proof_attempt": number}
    submission = store.read(run_id, "submission", required=True)
    snapshot = json.loads(store.read_blob(submission["checkpoint"]))
    if any(snapshot[name] for name in ("index_patch", "working_patch", "untracked")):
        raise Conflict("submission checkpoint is dirty")
    if (snapshot["base_sha"], snapshot["head_sha"]) != (request["base_sha"], submission["head_sha"]):
        raise Conflict("submission checkpoint identity differs")
    argv = ["git", "diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames",
            request["base_sha"], submission["head_sha"], "--"]
    output = workspace.run([
        "python", "-IB", "-c",
        "import base64,subprocess,sys; print(base64.b64encode(subprocess.check_output(sys.argv[1:])).decode())",
        *argv,
    ], limit=4 * 1024 * 1024)
    if output["exit_code"] or output["truncated"]:
        raise Conflict("submitted patch could not be exported")
    native = store.read_blob(attempt["native_events"])
    profile = json.loads(store.read_blob(request["configuration"]))
    usage = attempt["usage"]
    artifacts = {}
    for name, data in {
        "submission.bundle": decode(snapshot["bundle"]), "submission.patch": decode(output["output"].strip()),
        "native.jsonl": native, "trajectory.json": encode(trajectory(native, run_id, profile, usage)),
        "usage.json": encode({"usage": usage, "cost_usd": attempt["billing"]}),
    }.items():
        artifacts[name] = {"sha256": store.blob(data), "size": len(data)}
    partial = {
        **store.read(run_id, "dispatch-verify", required=True), **request["evaluation"],
        "submitted_sha": submission["head_sha"], "runtime": request["runtime"],
        "usage": usage, "cost_usd": attempt["billing"], "artifacts": artifacts, **proof,
    }
    return store.record(run_id, "harbor-export", partial)


def reconcile_grade(store, request, export, rollout, grade, bundle):
    run_id = request["run_id"]
    spec = request["evaluation"]
    submission = store.read(run_id, "submission", required=True)
    task = rollout["result"]
    expected_bundle = export["artifacts"]["submission.bundle"]["sha256"]
    stopped = store.read(run_id, "provider-stop", required=True)
    isolation = grade["isolation"]
    if (rollout["state"] != "finished" or rollout["id"] != spec["rollout"] or rollout["job_id"] != spec["job"]
            or task["id"] != spec["rollout"] or task["task_checksum"] != spec["task_checksum"]
            or rollout["lock"]["task"]["digest"] != spec["harbor_task_lock"]
            or export["grader_digest"] != request["grader_digest"]
            or (spec.get("reported_grader_required") and grade.get("grader_digest") != request["grader_digest"])
            or task.get("exception_info") or task["verifier_environment_mode"] != "separate" or not task["verifier_result"]
            or rollout.get("storage_error") or export["submitted_sha"] != submission["head_sha"]
            or grade["submitted_sha"] != export["submitted_sha"]
            or grade["bundle_sha256"] != expected_bundle or digest(bundle) != expected_bundle
            or stopped["removed"] is not True
            or not re.fullmatch(r"[0-9a-f]{12}", isolation.get("container", ""))
            or isolation["container"] == stopped["container"]
            or any(isolation.get(check) is not True for check in ISOLATION_CHECKS)):
        raise InvalidHarborGrade("Harbor grade provenance differs from the locked submission")
    reward = task["verifier_result"]["rewards"]["reward"]
    checks = grade["checks"]
    if (not isinstance(checks, dict) or checks.keys() != set(spec.get("grade_checks", GRADE_CHECKS))
            or any(type(result) is not bool for result in checks.values()) or type(reward) not in (float, int)):
        raise InvalidHarborGrade("Harbor protected grade has invalid check results")
    accepted = all(checks.values())
    if reward != int(accepted) or grade["status"] != ("accepted" if accepted else "rejected"):
        raise InvalidHarborGrade("Harbor result disagrees with the protected grade")
    verdict = {
        **spec, "checked_sha": grade["submitted_sha"], "grader_digest": request["grader_digest"],
        "protected_grade": store.blob(encode(grade)), "decision": grade["status"],
        "reward": reward, "bundle_sha256": expected_bundle,
    }
    store.record(run_id, "harbor-grade", verdict)
    return store.record(run_id, "verify", {
        "reason": verdict["decision"], "verdict": store.blob(encode(verdict)), "bundle": expected_bundle,
    })


def apply_fixture(store, request, verification):
    run_id = request["run_id"]
    if store.read(run_id, "verify", required=True) != verification or verification["reason"] != "accepted":
        raise Conflict("fixture requires the reconciled protected grade")
    grade = store.read(run_id, "harbor-grade", required=True)
    if store.read_blob(verification["verdict"]) != encode(grade):
        raise Conflict("fixture grade digest differs")
    op = "harbor-delivery:" + run_id
    claim = fixture_claim(grade, verification["verdict"])
    ledger = Ledger(store.root / "harbor-fixture.sqlite")
    receipt = ledger.lookup(op) or ledger.apply(op, claim)
    validate_receipt(receipt, op, claim)
    return store.record(run_id, "harbor-fixture", receipt)


def fixture_claim(grade, verdict):
    return digest(encode({"rollout": grade["rollout"], "submitted_sha": grade["checked_sha"],
                          "grader": grade["grader_digest"], "verdict": verdict}))


def reopen_harbor(store, request, manifest, artifacts):
    export, verdict, fixture = (json.loads(artifacts[name]) for name in
                                ("harbor-export", "harbor-grade", "harbor-fixture"))
    if request.get("workspace_marker") == TRUSTED_WORKSPACE_MARKER:
        number = export["workspace_proof_attempt"]
        key = f"attempt-{number}"
        proof = workspace_proof_digest(request, number, json.loads(artifacts[f"{key}-workspace-proof"]),
                                       json.loads(artifacts[f"{key}-marker"]), json.loads(artifacts["provider-start"]))
        if proof != export["workspace_proof"]:
            raise Conflict("retained Harbor workspace proof differs from the export")
    grade = json.loads(store.read_blob(verdict["protected_grade"]))
    lock = json.loads(store.read_blob(verdict["job_lock"]))
    if (lock["trials"][0]["task"]["digest"] != verdict["harbor_task_lock"]):
        raise Conflict("retained Harbor job lock differs")
    if (export["rollout"] != request["evaluation"]["rollout"] or export["rollout"] != verdict["rollout"]
            or export["submitted_sha"] != verdict["checked_sha"] or verdict["checked_sha"] != grade["submitted_sha"]
            or manifest["temporal_run_id"] != export["temporal_run_id"] or manifest["workflow_id"] != export["workflow_id"]
            or verdict["grader_digest"] != request["grader_digest"] or export["runtime"] != request["runtime"]):
        raise Conflict("retained Harbor identity differs")
    for name, artifact in export["artifacts"].items():
        if len(store.read_blob(artifact["sha256"])) != artifact["size"]:
            raise Conflict(f"retained Harbor artifact size differs: {name}")
    bundle = export["artifacts"]["submission.bundle"]["sha256"]
    if bundle != grade["bundle_sha256"] or bundle != digest(artifacts["submitted_bundle"]):
        raise Conflict("retained Harbor bundle differs")
    verification_digest = digest(artifacts["independent_verdict"])
    claim = fixture_claim(verdict, verification_digest)
    validate_receipt(fixture, "harbor-delivery:" + request["run_id"], claim)
