import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from time import sleep, time

from factory.agent_profile import resolve
from factory.checkpoint import decode
from factory.codex_delivery import dispatch
from factory.codex_workspace import seed
from factory.codex_transport import Proxy
from factory.evidence import Conflict, Evidence, digest, encode, validate_run_id


ROOT = Path(__file__).resolve().parents[2]
OUTCOME = """\
Add one read-only CLI for an operator to retrieve the verified Git patch for a
PR 02 records-delivery run ID. Implement the interface:
  PYTHONPATH=src python -m factory.verified_patch --evidence ARCHIVE RUN_ID
For a completed accepted delivery, emit the byte-exact binary-capable Git diff
from its recorded base to its verified submission, with standard Git paths
and no rename detection. Read the existing PR 02 archive format. Recheck
evidence integrity, verdict, and checked revision before emitting any patch.
Return nonzero with a clear stderr diagnostic containing 'corrupt' for corrupt
evidence, or 'missing' for a missing run or required artifact. Leave the evidence
archive untouched. Add focused behavioral tests and minimal usage documentation.
The target starts at the PR 02 merge. The independent grader is outside your
workspace. You may use the existing records fixture to construct your own tests.
"""


def compose(project, env, *args, capture=False):
    return subprocess.run(
        ["docker", "compose", "-p", project, "-f", "compose.yaml", "-f", "compose.recovery.yaml",
         "-f", "compose.codex.yaml", *args],
        cwd=ROOT, env=env, check=True, stdout=subprocess.PIPE if capture else None,
    ).stdout


def grade(store, request, grader, fixture_root, fixture_run, *, receipt="verify"):
    run_id = request["run_id"]
    if (result := store.read(run_id, receipt)) is not None:
        return result
    submission = store.read(run_id, "submission")
    snapshot = json.loads(store.read_blob(submission["checkpoint"]))
    if (submission["head_sha"] != snapshot["head_sha"] or snapshot["base_sha"] != request["base_sha"]
            or any(snapshot[name] for name in ("index_patch", "working_patch", "untracked"))
            or digest(grader) != request["grader_digest"]):
        raise Conflict("submission or grader changed")
    namespace = {}
    exec(compile(grader, "<pinned acceptance check>", "exec"), namespace)
    verdict = namespace["acceptance"](request, snapshot, fixture_root, fixture_run)
    return store.record(run_id, receipt, {
        "reason": verdict["decision"], "verdict": store.blob(encode(verdict)),
        "bundle": store.blob(decode(snapshot["bundle"])),
    })


def main(args):
    run_id = validate_run_id(args.run_id)
    if not args.approved_by.strip() or not 1 <= args.minutes <= 30 or not 1 <= args.attempts <= 2:
        raise ValueError("record maintainer approval and a cap of at most 30 minutes")
    root = args.evidence.resolve()
    root.mkdir(parents=True, exist_ok=True)
    project = f"factory-live-{run_id}"
    env = {**os.environ, "OUTCOME_EVIDENCE_ROOT": str(root)}
    store = Evidence(root)
    grader = args.grader.read_bytes()
    if digest(grader) != args.grader_sha:
        raise Conflict("grader did not match its approved digest")
    proxy_args = (args.proxy_url, args.proxy_key_file, args.proxy_ca)
    if any(proxy_args) and not all(proxy_args):
        raise ValueError("provide the proxy URL, private key file and CA together")
    proxy = Proxy(*proxy_args) if all(proxy_args) else None
    transport = proxy.pin(store) if proxy else {"kind": "direct", "auth_mode": "chatgpt"}
    admission = {
        "schema_version": 1, "run_id": run_id, "approved_by": args.approved_by, "outcome": OUTCOME,
        "target": "ufJmacca/factory-v2", "base_sha": args.base, "factory_sha": args.factory_sha,
        "grader_digest": args.grader_sha, "fixture_run": args.fixture_run, "cap_minutes": args.minutes, "cap_attempts": args.attempts, "transport": transport,
    }
    with store.lock(run_id):
        store.record(run_id, "approval", admission)
        request = store.read(run_id, "request")
        if request is None:
            profile = resolve(ROOT, args.factory_sha, store)
            store.blob(grader)
            compose(project, env, "build", "codex-workspace", "outcome-worker")
            image = subprocess.check_output([
                "docker", "image", "inspect", "--format", "{{.Id}}", f"{project}-codex-workspace",
            ], text=True).strip()
            worker = subprocess.check_output([
                "docker", "image", "inspect", "--format", "{{.Id}}", f"{project}-outcome-worker",
            ], text=True).strip()
            request = store.admit({
                **admission, "configuration": store.blob(encode(profile)),
                "workspace_image": image, "worker_image": worker,
                "base_checkpoint": store.blob(encode(seed(ROOT, args.base))),
                "deadline_epoch": time() + min(args.minutes, profile["profile"]["timeout_minutes"]) * 60,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "max_attempts": min(args.attempts, profile["profile"]["max_attempts"]),
            })
    compose(project, env, "up", "-d", "--no-build", "--wait", "--wait-timeout", "60", "outcome-worker")
    try:
        compose(project, env, "exec", "-T", "outcome-worker",
                "python", "-m", "factory.outcome_worker", "submit", run_id)
        while time() < request["deadline_epoch"] + 65:
            if store.read(run_id, "manifest") is not None:
                content = compose(project, env, "exec", "-T", "outcome-worker", "python", "-m",
                                  "factory.outcome_worker", "reopen", "/outcome-evidence", run_id, capture=True)
                print(content.decode().strip())
                return
            if time() < request["deadline_epoch"]:
                for attempt in range(1, request["max_attempts"] + 1):
                    key = f"attempt-{attempt}"
                    if (ticket := store.read(run_id, f"dispatch-{key}")) and store.read(run_id, key) is None:
                        try:
                            result = dispatch(store, request, ticket, args.codex, args.auth_home, proxy)
                        except (OSError, ValueError, subprocess.SubprocessError) as error:
                            result = store.record(run_id, key, {
                                "attempt": attempt, "configuration": request["configuration"],
                                "reason": "runner_infrastructure_failed", "error_type": type(error).__name__,
                                "usage": None, "billing": None,
                            })
                        print(f"Attempt {attempt} {result['reason']}", flush=True)
                if store.read(run_id, "dispatch-verify") and store.read(run_id, "verify") is None:
                    try:
                        grade(store, request, grader, args.fixture.resolve(), args.fixture_run)
                    except (OSError, ValueError, subprocess.SubprocessError) as error:
                        store.record(run_id, "verify", {
                            "reason": "verifier_infrastructure_failed", "error_type": type(error).__name__,
                        })
            sleep(0.25)
        raise TimeoutError("Temporal did not publish a terminal manifest")
    finally:
        compose(project, env, "down", "--remove-orphans", "--timeout", "5")


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--base", default="31f67b2ea40d4fa4570b26dd9b175e9fe423f416")
    parser.add_argument("--factory-sha", required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--minutes", type=int, default=30)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--grader", type=Path, required=True)
    parser.add_argument("--grader-sha", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--fixture-run", required=True)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--proxy-url")
    parser.add_argument("--proxy-key-file", type=Path)
    parser.add_argument("--proxy-ca", type=Path)
    parser.add_argument("--auth-home", type=Path, default=Path.home() / ".codex")
    main(parser.parse_args())
