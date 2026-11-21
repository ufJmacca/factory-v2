import json

from factory.checkpoint import decode
from factory.codex_evidence import reopen
from factory.evidence import Conflict, digest, encode


def retain_submission(original, run_id, destination):
    manifest = reopen(original, run_id)
    recovery = reopen(original, run_id, "verification-recovery")
    acceptance = original.read(run_id, "maintainer-acceptance")
    request = original.read(run_id, "request")
    records = {
        "source_request": request, "original_manifest": manifest, "verification_recovery": recovery,
        "maintainer_acceptance": acceptance,
    }
    source = {name: destination.blob(original.read_blob(ref)) for name, ref in recovery["artifacts"].items()}
    source.update({name: destination.blob(encode(record)) for name, record in records.items()})
    attest(destination, source, repo=request["target"], actor=request["approved_by"], revision=recovery["submitted_sha"])
    return source, request, recovery["submitted_sha"]


def attest(store, source, *, repo, actor, revision):
    request, manifest, recovery, acceptance, checkpoint, verdict = (
        json.loads(store.read_blob(source[name])) for name in (
            "source_request", "original_manifest", "verification_recovery", "maintainer_acceptance",
            "submission_checkpoint", "independent_verdict",
        )
    )
    bundle = store.read_blob(source["submitted_bundle"])
    if (
        recovery["status"] != "verified" or recovery["reason"] != "accepted"
        or manifest["reason"] != "verifier_infrastructure_failed"
        or recovery["artifacts"]["original_manifest"] != source["original_manifest"]
        or any(source[name] != ref for name, ref in recovery["artifacts"].items())
        or digest(encode(request)) != manifest["request_digest"]
        or manifest["request_digest"] != recovery["request_digest"]
        or request["target"] != repo or request["approved_by"] != actor or acceptance["actor"] != actor
        or request["run_id"] != manifest["run_id"] or request["run_id"] != recovery["run_id"]
        or request["run_id"] != acceptance["run_id"]
        or acceptance["decision"] != "accepted"
        or acceptance["verification_manifest_digest"] != source["verification_recovery"]
        or any(value != revision for value in (
            manifest["submitted_sha"], recovery["submitted_sha"], acceptance["submitted_sha"],
            checkpoint["head_sha"], verdict["checked_sha"],
        ))
        or checkpoint["last_completed_step"] != "submission"
        or checkpoint["base_sha"] != request["base_sha"]
        or decode(checkpoint["bundle"]) != bundle
        or verdict["decision"] != "accepted" or verdict["grader_digest"] != request["grader_digest"]
        or not verdict["checks"] or not all(check["passed"] for check in verdict["checks"])
    ):
        raise Conflict("P03 recovery and maintainer acceptance do not bind this product submission")
    return checkpoint, bundle


def load_candidate(store, request):
    source = request["source"]
    prior = request.get("predecessor")
    checkpoint, bundle = attest(
        store, source, repo=request["repo"], actor=request["approver"],
        revision=prior["revision"] if prior else request["revision"],
    )
    if prior is None:
        return checkpoint, bundle
    checkpoint = json.loads(store.read_blob(source["integrated_checkpoint"]))
    bundle = store.read_blob(source["integrated_bundle"])
    verdict = json.loads(store.read_blob(source["integration_verdict"]))
    if (
        checkpoint["base_sha"] != prior["base_sha"]
        or checkpoint["head_sha"] != request["revision"] or verdict["checked_sha"] != request["revision"]
        or checkpoint["index_patch"] or checkpoint["working_patch"] or checkpoint["untracked"]
        or decode(checkpoint["bundle"]) != bundle or verdict["grader_digest"] != source["grader"]
        or verdict["decision"] != "accepted" or not verdict["checks"] or not all(row["passed"] for row in verdict["checks"])
    ):
        raise Conflict("integrated checkout or independent verdict does not match the approved product head")
    return checkpoint, bundle
