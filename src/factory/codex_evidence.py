import json

from factory.checkpoint import decode
from factory.delivery_steps import require
from factory.evidence import digest, encode


def reopen(store, run_id, record="manifest"):
    manifest = store.read(run_id, record)
    require(manifest is not None and manifest["run_id"] == run_id, "terminal manifest missing")
    request = store.read(run_id, "request")
    require(digest(encode(request)) == manifest["request_digest"], "request digest differs")
    artifacts = {name: store.read_blob(ref) for name, ref in manifest["artifacts"].items()}
    if manifest["reason"] != "pinned_configuration_unavailable":
        profile = json.loads(artifacts["configuration"])
        store.read_blob(profile["source_digest"])
        for reference in profile["skills"].values():
            store.read_blob(reference)
    if record == "verification-recovery":
        original = store.read(run_id, "manifest")
        require(encode(original) == artifacts["original_manifest"]
                and original["reason"] == "verifier_infrastructure_failed"
                and original["submitted_sha"] == manifest["submitted_sha"], "original terminal record differs")
    if manifest["status"] == "verified":
        red, green, submission, verdict = (
            json.loads(artifacts[name]) for name in ("red", "green", "submission", "independent_verdict")
        )
        require(red["argv"] == green["argv"] == submission["argv"], "behavioral check differs")
        for phase, record, expected in (("red", red, 1), ("green", green, 0), ("submission", submission, 0)):
            checkpoint = json.loads(store.read_blob(record["checkpoint"]))
            require(checkpoint["last_completed_step"] == phase and checkpoint["base_sha"] == request["base_sha"]
                    and checkpoint["head_sha"] == record["head_sha"], "checkpoint revision differs")
            require(json.loads(store.read_blob(record["test"]))["exit_code"] == expected, "test verdict differs")
        snapshot = json.loads(store.read_blob(submission["checkpoint"]))
        require(verdict["checked_sha"] == snapshot["head_sha"] == manifest["submitted_sha"]
                and verdict["decision"] == "accepted" and verdict["grader_digest"] == request["grader_digest"]
                and artifacts["submitted_bundle"] == decode(snapshot["bundle"]), "independent verdict differs")
        require(any(name.endswith("_events") for name in artifacts), "native event stream missing")
    return manifest

