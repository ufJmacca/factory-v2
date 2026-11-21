from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory.delivery_steps import reopen, require
from factory.evidence import Evidence, digest
import json


def verify_drill(directory: Path, crash: str, fresh: str):
    def read(name):
        return json.loads((directory / f"{name}.json").read_bytes())

    before = Evidence(directory / "before")
    archive = Evidence(directory / "archive")
    started = read("crash-start")
    require(started == read("crash-duplicate-running") == read("crash-duplicate-closed"),
            "identical requests resolved to different executions")
    require((directory / "crash-conflict.log").read_text().strip() == "conflicting evidence: request.json",
            "conflicting request failed for the wrong reason")
    require(before.read(crash, "receipt") is None, "worker retained a receipt before dying")
    require(before.read(crash, "manifest") is None, "worker published a manifest before dying")
    pending = read("crash-pending")
    require(pending["status"] == "RUNNING" and pending["temporal_run_id"] == started["temporal_run_id"],
            "original workflow did not remain open after the kill")
    require(any(item["name"] == "deliver_records" and item["attempt"] == 1
                for item in pending["pending_activities"]), "delivery was not pending at the kill")
    require({p.name for p in (archive.root / "runs").iterdir()} == {crash, fresh},
            "expected exactly two deliveries")
    require(read("fixture-before-restart")["effect_count"] == 1, "effect was absent at worker loss")
    ledger = read("fixture-after")
    require(ledger["effect_count"] == 2, "expected one effect per delivery")
    require(len(ledger["effects"]) == 2, "fixture ledger does not match effect count")
    ledger_by_id = {effect["operation_id"]: effect for effect in ledger["effects"]}
    prepared = before.read(crash, "prepared")
    environment = digest((directory / "environment.txt").read_bytes())
    results = {}
    for case, run_id, attempt in (("crash", crash, 2), ("fresh", fresh, 1)):
        manifest = reopen(archive, run_id)
        require(manifest == read(f"{case}-result"), "workflow and archive disagree")
        submitted = read(f"{case}-start")
        for key in ("request_digest", "run_id", "workflow_id", "temporal_run_id"):
            require(manifest[key] == submitted[key], f"submission and manifest differ: {key}")
        require(manifest["environment_digest"] == environment, "environment digest differs")
        require(manifest["status"] == "delivered" and manifest["verdict"] == "accepted", "delivery failed")
        require(manifest["delivery_attempt"] == attempt, "wrong activity attempt")
        require(manifest["model_calls"] == 0 and not manifest["missing_artifacts"], "incomplete proof")
        receipt = archive.read(run_id, "receipt")
        require(ledger_by_id[receipt["operation_id"]] == {**receipt, "apply_requests": 1},
                "fixture was applied more than once or reports a different receipt")
        results[case] = manifest
        print(f"{case} {run_id} workflow={manifest['workflow_id']} temporal_run={manifest['temporal_run_id']}")
        print(f"  head={manifest['submitted_sha']} verdict=accepted attempt={attempt} effects=1")
        print(f"  reopened and checked {len(manifest['artifacts'])} artifact hashes")

    recovered, independent = results["crash"], results["fresh"]
    require(recovered["temporal_run_id"] != independent["temporal_run_id"], "fresh execution was reused")
    require(recovered["recovery"]["status"] == "restored", "retry did not restore a checkpoint")
    require(recovered["recovery"]["checkpoint_step"] == "submission", "wrong recovery checkpoint")
    require(independent["recovery"]["status"] == "initial", "fresh run took the recovery path")
    require(recovered["submitted_sha"] == independent["submitted_sha"] == prepared["submitted_sha"],
            "original submitted head changed")
    before_artifacts = prepared["artifacts"]
    require(before_artifacts["verifier_verdict"] == recovered["artifacts"]["verifier_verdict"],
            "original verdict digest changed")
    verdict = before.read_blob(before_artifacts["verifier_verdict"])
    require(verdict == archive.read_blob(recovered["artifacts"]["verifier_verdict"])
            == archive.read_blob(independent["artifacts"]["verifier_verdict"]), "verdict bytes changed")
    snapshot = json.loads(before.read_blob(before_artifacts["submission_checkpoint"]))
    require(recovered["recovery"]["tree"] == snapshot["tree"], "restored Git tree changed")
    require(recovered["recovery"]["checkpoint_digest"] == before_artifacts["submission_checkpoint"],
            "recovery used a different checkpoint")
    require((directory / "crash-restored-head.txt").read_text().strip() == prepared["submitted_sha"],
            "candidate checkout inspected after recovery had the wrong head")
    require((directory / "crash-restored-untracked.txt").read_bytes() == b"one\ntwo",
            "candidate checkout lost its required untracked file")
    crash_receipt = archive.read(crash, "receipt")
    require(read("fixture-before-restart")["effects"] == [{**crash_receipt, "apply_requests": 1}],
            "recovery attached a different receipt from the one at worker loss")
    print("Verified both terminal manifests after checkout removal. The fixture has two effects and two apply requests.")


if __name__ == "__main__":
    verify_drill(Path(sys.argv[1]), *sys.argv[2:])
