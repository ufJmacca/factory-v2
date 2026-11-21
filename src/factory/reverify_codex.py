import argparse
import json
import os
from pathlib import Path

from factory.codex_evidence import reopen
from factory.evidence import Conflict, Evidence, encode, timestamp
from factory.live_codex import grade


def recover(store: Evidence, run_id: str, fixture: Path) -> dict:
    with store.lock(run_id):
        prior = reopen(store, run_id)
        if prior["reason"] != "verifier_infrastructure_failed" or not prior["submitted_sha"]:
            raise Conflict("verification recovery requires an already-submitted revision and a grader setup failure")
        if store.read(run_id, "verification-recovery") is not None:
            return reopen(store, run_id, "verification-recovery")
        request = store.read(run_id, "request")
        grader = store.read_blob(request["grader_digest"])
        result = grade(
            store, request, grader, fixture, request["fixture_run"], receipt="verification-recovery-result",
        )
        artifacts = {
            **prior["artifacts"], "original_manifest": store.blob(encode(prior)),
            "independent_verdict": result["verdict"], "submitted_bundle": result["bundle"],
        }
        store.record(run_id, "verification-recovery", {
            **prior, "status": "verified" if result["reason"] == "accepted" else "rejected",
            "reason": result["reason"], "completed_at": timestamp(), "artifacts": artifacts,
        })
        return reopen(store, run_id, "verification-recovery")


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(recover(Evidence(args.evidence.resolve()), args.run_id, args.fixture.resolve()), sort_keys=True))
