"""Retain reviewed pilot versions and verify the effective inputs on main."""

import argparse
import json
import os
from pathlib import Path
import subprocess

from factory.agent_profile import resolve
from factory.checkpoint import git
from factory.evidence import Conflict, Evidence, digest, encode
from factory.fresh_pilot import RED_ARGV_PILOT_ID, require_red_argv_isolation
from factory.harbor_experiment import report
from factory.harbor_receipt import runtime_pin
from scripts.promotion import Promotion, named, require, revision


SOURCE_INPUTS = (
    ".dockerignore", "agents.toml", "pyproject.toml", "uv.lock", "grading/Dockerfile", "grading/checks.json",
    "src/factory", "compose.yaml", "compose.codex.yaml", "compose.recovery.yaml", "compose.harbor.yaml",
    "compose.harbor-bridge.yaml", "evaluation/harbor", "scripts/probe_harbor_runner.py",
    "scripts/probe_codex_cli.py", "scripts/verify-fresh-pilot.sh",
)


def source_inputs(repo, sha):
    return digest(git(repo, "ls-tree", "-rz", sha, "--", *SOURCE_INPUTS))


def compare_inputs(store, repo, candidate, reviewed_sha, worker_image):
    require(revision(reviewed_sha), "comparison requires the full Git commit")
    version = Promotion(store).version(candidate)
    tested = version["runtime"]["workflow_build"]
    pin = source_inputs(repo, tested)
    require(source_inputs(repo, reviewed_sha) == version["source_inputs_sha256"] == pin,
            "the candidate worker or helper inputs changed")
    expected = json.loads(store.read_blob(version["configuration"]))
    profile = resolve(repo, reviewed_sha, store)
    require(profile == {**expected, "factory_sha": reviewed_sha},
            "the candidate bot selector or skill inputs changed")
    require(runtime_pin(profile, tested, worker_image, version["runtime"]["transport"]) == version["runtime"],
            "the candidate worker image or runtime changed")
    return {
        "schema_version": 1, "candidate": candidate, "tested_sha": tested, "reviewed_sha": reviewed_sha,
        "source_inputs_sha256": pin, "worker_image": worker_image,
    }


def verify_merged(store, repo, candidate, merged_sha, worker_image):
    require(revision(merged_sha), "merge verification requires a full Git commit")
    try:
        git(repo, "merge-base", "--is-ancestor", merged_sha, "refs/remotes/origin/main")
    except subprocess.CalledProcessError as error:
        raise Conflict("revision must be present in fetched origin/main") from error
    proof = compare_inputs(store, repo, candidate, merged_sha, worker_image)
    del proof["reviewed_sha"]
    return {**proof, "merged_sha": merged_sha}


def record_review(store, repo, archive, tested, experiment_sha, evidence_sha, reviewer):
    require(revision(tested) and named(reviewer), "promotion review requires a tested commit and named reviewer")
    frozen = archive.read("experiment", "frozen", required=True)
    manifest = (archive.root / "experiment.json").read_bytes()
    require(manifest == encode(frozen) and digest(manifest) == experiment_sha
            and frozen.get("mode") == "live" and frozen.get("pilot_kind") == RED_ARGV_PILOT_ID,
            "the recorded live experiment differs from the reviewed experiment")
    require((archive.root / "tested-sha.txt").read_text().strip() == tested
            and not (archive.root / "tested-status.txt").read_text().strip(), "the tested worker source is not pinned")
    summary = (archive.root / "summary.json").read_bytes()
    decision = json.loads(summary)
    recomputed = report(archive, frozen["assignments"], live=True, red_argv_pilot=True)
    require(digest(summary) == evidence_sha and decision["experiment_digest"] == experiment_sha
            and recomputed["decision"] == "eligible_for_review"
            and all(decision.get(key) == value for key, value in recomputed.items()),
            "the retained decision does not qualify the candidate for promotion review")
    configuration = frozen["configuration"]
    arms = configuration["arms"]
    require_red_argv_isolation(arms)
    workspace_images = {task["workspace_image"] for task in frozen["tasks"].values()}
    require(len(workspace_images) == 1, "the versions must share a workspace image")
    workspace_image = next(iter(workspace_images))
    profile_ref = arms["baseline"]["configuration"]
    profile = json.loads(archive.read_blob(profile_ref))
    require(profile == {**resolve(repo, tested, store), "factory_sha": profile["factory_sha"]},
            "the tested selector or skills differ from the archived configuration")
    for reference in (profile_ref, profile["source_digest"], *profile["skills"].values(),
                      configuration["transport"]["ca_digest"]):
        store.blob(archive.read_blob(reference))

    versions = {}
    pins = source_inputs(repo, tested)
    for name, arm in arms.items():
        require(arm["runtime"]["workflow_build"] == tested, "the arm has a different tested worker")
        versions[name] = store.blob(encode({
            "schema_version": 1, **arm, "workspace_image": workspace_image, "source_inputs_sha256": pins,
            "tool_definition_sha256": frozen["tool_definition_sha256"][name],
        }))
    review = {
        "schema_version": 1, "decision": "promote", "reviewer": reviewer, "tested_sha": tested,
        "experiment_sha256": store.blob(manifest), "evidence_sha256": store.blob(summary),
        **versions,
    }
    return {**review, "review_sha256": store.blob(encode(review))}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    review = commands.add_parser("record-review")
    review.add_argument("--archive", type=Path, required=True)
    review.add_argument("--tested-sha", required=True)
    review.add_argument("--experiment-sha256", required=True)
    review.add_argument("--evidence-sha256", required=True)
    review.add_argument("--reviewer", required=True)
    for command in ("compare", "verify-merged"):
        compare = commands.add_parser(command)
        compare.add_argument("--candidate", required=True)
        compare.add_argument("--commit", required=True)
        compare.add_argument("--worker-image", required=True)
    args = parser.parse_args()
    store = Evidence(args.evidence.resolve())
    repo = args.repo.resolve()
    if args.command == "record-review":
        output = record_review(store, repo, Evidence(args.archive.resolve()), args.tested_sha,
                               args.experiment_sha256, args.evidence_sha256, args.reviewer)
    elif args.command == "compare":
        output = compare_inputs(store, repo, args.candidate, args.commit, args.worker_image)
    else:
        proof = verify_merged(store, repo, args.candidate, args.commit, args.worker_image)
        output = {**proof, "parity_sha256": store.blob(encode(proof))}
    print(encode(output).decode(), end="")


if __name__ == "__main__":
    main()
