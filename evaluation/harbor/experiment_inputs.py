"""Prepare the isolated configuration revisions and pin the Phase 05 source chain."""
import json
import os
from pathlib import Path
import subprocess
import sys

from factory.agent_profile import resolve
from factory.checkpoint import git
from factory.codex_workspace import seed
from factory.evidence import Conflict, Evidence, encode
from factory.harbor_experiment import compare_profiles
from factory.harbor_receipt import runtime_pin
from factory.proposal_evidence import workspace_retry
from scripts.probe_harbor_runner import proxy_from_env


def prepare(root, archive, baseline, build, worker, mode, *, source_repo=None, cli_build=None,
            trusted_transport=None, checkpoints=None):
    store = Evidence(root)
    original = Path.cwd()
    source_repo = source_repo or original
    source = original / "docs/p05"
    proposal, candidate, request = (
        json.loads((source / path).read_bytes()) for path in
        ("proposal.json", "candidate-manifest.json", "experiment-request.json")
    )
    patch = (source / "candidate.patch").read_bytes()
    path = candidate["path"]
    historical = request["baseline"]
    before, after = historical["skill_sha256"], candidate["sha256"]
    if (store.blob(encode(proposal)) != request["proposal_sha256"]
            or store.blob(encode(candidate)) != request["candidate_manifest_sha256"]
            or proposal["candidate_manifest_sha256"] != request["candidate_manifest_sha256"]
            or store.blob(patch) != candidate["diff_sha256"]
            or proposal["id"] != request["proposal_id"] or proposal["experiment_request_id"] != request["id"]
            or proposal["baseline"] != historical or candidate["baseline"] != historical
            or proposal["prediction"] != request["prediction"] or request["pairs"] != 3
            or request["dispatch_authorized"] is not False
            or request["changed_input"] != {"path": path, "baseline_sha256": before, "candidate_sha256": after}):
        raise Conflict("Phase 05 proposal, candidate and request disagree")
    pointer, retry = proposal["event_pointers"]
    archived = Evidence(archive)
    recovered = archived.read(pointer["run_id"], "verification-recovery", required=True)
    native = archived.read_blob(pointer["sha256"])
    pair = workspace_retry(native, pointer["line"], retry["line"])
    if (not pair or [item.item_id for item in pair] != [pointer["item_id"], retry["item_id"]]
            or pointer["sha256"] != retry["sha256"]
            or recovered["artifacts"]["attempt-1_events"] != pointer["sha256"]):
        raise Conflict("Phase 05 source events differ from the retained trace")

    repo = root / "configuration-repo"
    if not repo.exists():
        git(source_repo, "clone", "--quiet", "--shared", "--no-checkout", ".", str(repo))
        git(repo, "checkout", "--quiet", "--detach", baseline)
        git(repo, "apply", "--index", "-", input=patch)
        git(repo, "-c", "user.name=Factory evaluation", "-c", "user.email=factory@invalid",
            "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Apply the reviewed Phase 05 proposal",
            env={"GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"})
    revision = git(repo, "rev-parse", "HEAD").decode().strip()
    if (git(repo, "rev-parse", "HEAD^").decode().strip() != baseline
            or git(repo, "diff", "--name-only", baseline, revision).decode().splitlines() != [path]):
        raise Conflict("candidate Git revision changed more than the proposed skill")
    archived_profile = resolve(source_repo, historical["factory_commit"], store)
    baseline_profile = resolve(repo, baseline, store)
    candidate_profile = resolve(repo, revision, store)
    if (archived_profile["source_digest"], archived_profile["skills"][path]) != (historical["selector_sha256"], before):
        raise Conflict("historical selector or skill differs from Phase 05")
    if baseline_profile["source_digest"] != historical["selector_sha256"]:
        raise Conflict("current selector differs from Phase 05")
    compare_profiles(baseline_profile, candidate_profile, path, before, after)

    cli = cli_build or subprocess.check_output([os.environ.get("FACTORY_CODEX_CLI", "codex"), "--version"],
                                               text=True).strip()
    fixed = request["fixed_inputs"]
    if (cli, baseline_profile["model"], baseline_profile["reasoning_effort"], baseline_profile["tools"],
            baseline_profile["auth_mode"], baseline_profile["api_fallback"]) != (
            fixed["cli_build"], fixed["model"], fixed["reasoning_effort"], fixed["tools"], fixed["auth_mode"], False):
        raise Conflict("CLI, model, reasoning, tool permissions or billing mode drifted")
    transport = (trusted_transport or proxy_from_env().pin(store)) if mode == "live" else {"kind": "scripted-responses"}
    if mode == "live" and transport != archived.read(pointer["run_id"], "request", required=True)["transport"]:
        raise Conflict("live comparison requires the original Phase 03 HTTPS Hub route")
    arms = {}
    for arm, profile in (("baseline", baseline_profile), ("candidate", candidate_profile)):
        arms[arm] = {"factory_sha": profile["factory_sha"], "configuration": store.blob(encode(profile)),
                     "runtime": runtime_pin(profile, build, worker, transport)}
    tasks = store.read("experiment", "tasks", required=True)
    bases = {
        name: store.blob(encode(checkpoints[name] if checkpoints is not None else seed(repo, task["base_sha"])))
        for name, task in tasks.items()
    }
    store.record("experiment", "configuration", {
        "arms": arms, "base_checkpoints": bases, "transport": transport, "tools": baseline_profile["tools"],
        "source": {"proposal": request["proposal_sha256"], "candidate_manifest": request["candidate_manifest_sha256"],
                   "candidate_diff": candidate["diff_sha256"], "original_request": store.blob(encode(request)),
                   "historical_workspace_image": fixed["workspace_image"], "historical_worker_image": fixed["worker_image"],
                   "historical_grader": fixed["grader_sha256"], "events": proposal["event_pointers"]},
    })


if __name__ == "__main__":
    root, archive, baseline, build, worker, mode = sys.argv[1:]
    prepare(Path(root), Path(archive), baseline, build, worker, mode)
