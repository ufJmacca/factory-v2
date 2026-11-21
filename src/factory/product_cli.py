import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import sleep, time

from factory.checkpoint import capture, restore
from factory.evidence import Conflict, Evidence, encode
from factory.live_codex import compose, grade
from factory.product import (
    ACTIONS, ProductRunner, frozen_review, record_approval, record_observation, reopen_product, scope,
    validate_request, verify_ci,
)
from factory.product_github import GitHub
from factory.product_source import load_candidate, retain_submission


def admit(store, args):
    original = Evidence(args.source.resolve())
    source, source_request, revision = retain_submission(original, args.run_id, store)
    check = {"kind": "status", "name": args.check, "actor": args.check_actor}
    request = {
        "schema_version": 1, "run_id": args.run_id, "repo": source_request["target"], "base": "main",
        "title": "Retrieve verified records delivery patches",
        "revision": revision, "source": source, "approver": source_request["approved_by"],
        "reviewer": args.reviewer, "review_source": args.review_source,
        "checks": [check], "ci_publish": check, "environment": None,
        "deadline": args.deadline, "observation_seconds": args.observation_seconds,
    }
    validate_request(request)
    return store.admit(request)


def admit_integration(store, args):
    prior = reopen_product(store, args.prior_run)
    previous = store.read(args.prior_run, "request")
    if prior["status"] != "observed" or previous.get("predecessor"):
        raise Conflict("integration requires an observed original product run")
    submission = store.read(args.run_id, "submission")
    review = store.read(args.run_id, "frozen-review")
    checkpoint = json.loads(store.read_blob(submission["checkpoint"]))
    request = {
        **previous, "run_id": args.run_id, "revision": args.revision, "deadline": args.deadline,
        "predecessor": {"run_id": args.prior_run, "revision": prior["accepted_revision"],
                        "base_sha": checkpoint["base_sha"]},
        "source": {**previous["source"], "integrated_checkpoint": submission["checkpoint"],
                   "integrated_bundle": review["bundle"], "integration_verdict": review["verdict"],
                   "prior_product_manifest": store.blob(encode(prior))},
    }
    validate_request(request)
    load_candidate(store, request)
    return store.admit(request)


def run_grader(store, source, run_id, base, image, fixture):
    original = json.loads(store.read_blob(source["source_request"]))
    return grade(store, {**original, "run_id": run_id, "base_sha": base, "workspace_image": image},
                 store.read_blob(source["grader"]), fixture.resolve(), original["fixture_run"],
                 receipt="frozen-review")


def build_grader(project, env):
    compose(project, env, "build", "codex-workspace")
    return subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", f"{project}-codex-workspace"],
        env=env, text=True,
    ).strip()


def prepare_integration(store, args):
    previous = store.read(args.prior_run, "request")
    validate_request(previous)
    snapshot = capture(args.checkout.resolve(), args.base_sha)
    if snapshot["head_sha"] != args.revision or any(snapshot[name] for name in ("index_patch", "working_patch", "untracked")):
        raise Conflict("integration checkout does not match the specified clean revision")
    store.record(args.run_id, "submission", {
        "head_sha": args.revision, "checkpoint": store.blob(encode(snapshot)),
    })
    project = f"factory-product-review-{args.run_id}"
    env = {**os.environ, "OUTCOME_EVIDENCE_ROOT": str(store.root)}
    result = run_grader(store, previous["source"], args.run_id, args.base_sha, build_grader(project, env), args.fixture)
    verdict = json.loads(store.read_blob(result["verdict"]))
    if verdict["checked_sha"] != args.revision or verdict["decision"] != "accepted":
        raise Conflict("frozen grader did not accept the integration revision")
    return verdict


def preflight(store, request, project, env, fixture):
    run_id = request["run_id"]
    checkpoint, _ = load_candidate(store, request)
    compose(project, env, "build", "workspace", "codex-workspace", "outcome-worker")
    if store.read(run_id, "ci-preflight") is None:
        with TemporaryDirectory(prefix="factory-p04-ci-") as temporary:
            checkout = Path(temporary).resolve() / "checkout"
            restore(checkout, checkpoint)
            argv = ["uv", "run", "--locked", "--offline", "python", "-m", "unittest", "discover",
                    "-s", "tests", "-p", "test_verified_patch.py", "-v"]
            process = subprocess.run(
                ["docker", "compose", "-p", project, "run", "--rm", "--no-deps", "--pull", "never", "-T",
                 "-v", f"{checkout}:/workspace:ro", "workspace", *argv],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90,
            )
        store.record(run_id, "ci-preflight", {
            "revision": request["revision"], "check": request["ci_publish"], "argv": argv,
            "exit_code": process.returncode, "log": store.blob(process.stdout),
        })
    verify_ci(store, request, store.read(run_id, "ci-preflight"))
    if request["review_source"] == "frozen_grader":
        if fixture is None:
            raise Conflict("the frozen grader requires the original records fixture archive")
        if store.read(run_id, "frozen-review") is None:
            store.record(run_id, "submission", {
                "checkpoint": request["source"]["submission_checkpoint"], "head_sha": request["revision"],
            })
            run_grader(store, request["source"], run_id, checkpoint["base_sha"], build_grader(project, env), fixture)
        frozen_review(store, request, store.read(run_id, "frozen-review"))


@contextmanager
def temporal(project, env):
    try:
        compose(project, env, "up", "-d", "--no-build", "--wait", "--wait-timeout", "60", "outcome-worker")
        yield
    except BaseException:
        compose(project, env, "down", "--timeout", "5")
        raise
    else:
        compose(project, env, "down", "--volumes", "--timeout", "5")


def run_live(store, request, fixture):
    project = f"factory-product-{request['run_id']}"
    env = {**os.environ, "OUTCOME_EVIDENCE_ROOT": str(store.root)}
    if store.read(request["run_id"], "temporal-completion") is not None:
        return reopen_product(store, request["run_id"])
    preflight(store, request, project, env, fixture)
    with GitHub(store).prepare(request) as remote, temporal(project, env):
        for _ in range(2):
            compose(project, env, "exec", "-T", "outcome-worker", "python", "-m",
                    "factory.outcome_worker", "submit-product", request["run_id"])
        runner = ProductRunner(store, remote)
        last_status = None
        run_id = request["run_id"]
        while time() < request["deadline"] + 30:
            if store.read(run_id, "product-manifest") is not None:
                break
            if store.read(run_id, "dispatch-product") is not None:
                try:
                    result = runner.advance(run_id)
                    status = result["status"]
                    if status != last_status:
                        receipt = store.read(run_id, "receipt-pull_request")
                        pr = receipt["remote"] if receipt else store.read(run_id, "adopted-pull_request")
                        print(f"{run_id} {status}" + (f" {pr['url']}" if pr else ""), flush=True)
                        last_status = status
                except OSError:
                    print(f"{run_id} retrying a GitHub read", flush=True)
            sleep(5)
        report = reopen_product(store, run_id)
        compose(project, env, "exec", "-T", "outcome-worker", "python", "-m",
                "factory.outcome_worker", "wait-product", run_id)
        return report


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("run_id")
    commands = parser.add_subparsers(dest="command", required=True)
    admission = commands.add_parser("admit")
    admission.add_argument("--source", required=True, type=Path)
    admission.add_argument("--reviewer", required=True)
    admission.add_argument("--review-source", choices=("github", "frozen_grader"), required=True)
    admission.add_argument("--check", required=True)
    admission.add_argument("--check-actor", required=True)
    admission.add_argument("--deadline", required=True, type=float)
    admission.add_argument("--observation-seconds", required=True, type=int)
    prepare = commands.add_parser("prepare-integration")
    prepare.add_argument("--prior-run", required=True)
    prepare.add_argument("--checkout", required=True, type=Path)
    prepare.add_argument("--revision", required=True)
    prepare.add_argument("--base-sha", required=True)
    prepare.add_argument("--fixture", required=True, type=Path)
    integration = commands.add_parser("admit-integration")
    integration.add_argument("--prior-run", required=True)
    integration.add_argument("--revision", required=True)
    integration.add_argument("--deadline", required=True, type=float)
    approval = commands.add_parser("approve")
    approval.add_argument("action", choices=ACTIONS)
    approval.add_argument("--actor", required=True)
    approval.add_argument("--expires-at", required=True, type=float)
    approval.add_argument("--source", required=True)
    observe = commands.add_parser("observe")
    observe.add_argument("--actor", required=True)
    observe.add_argument("--pull-request", required=True, type=int)
    observe.add_argument("--feedback", required=True)
    observe.add_argument("--active-human-minutes", type=float)
    live = commands.add_parser("run")
    live.add_argument("--fixture", type=Path)
    commands.add_parser("reopen")
    args = parser.parse_args()
    store = Evidence(args.evidence.resolve())
    run_id = args.run_id
    if args.command == "admit":
        result = admit(store, args)
    elif args.command == "prepare-integration":
        result = prepare_integration(store, args)
    elif args.command == "admit-integration":
        result = admit_integration(store, args)
    else:
        request = store.read(run_id, "request")
        validate_request(request)
        if args.command == "approve":
            if args.actor != request["approver"] or not time() < args.expires_at <= request["deadline"]:
                raise Conflict("approval actor or expiry differs from the admitted authority")
            with store.lock(run_id):
                result = record_approval(store, run_id, {
                    **scope(request, args.action), "expires_at": args.expires_at,
                    "approved_at": time(), "source": args.source,
                })
        elif args.command == "observe":
            with store.lock(run_id):
                if store.read(run_id, "product-manifest"):
                    raise Conflict("the observation window is closed")
                result = record_observation(store, run_id, {
                    "actor": args.actor, "pull_request": args.pull_request, "revision": request["revision"],
                    "observed_at": time(), "feedback": args.feedback, "active_human_minutes": args.active_human_minutes,
                })
        elif args.command == "run":
            result = run_live(store, request, args.fixture)
        else:
            result = reopen_product(store, run_id)
    if args.command in {"admit", "admit-integration"}:
        result = {key: result[key] for key in ("run_id", "repo", "revision", "deadline", "reviewer")}
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
