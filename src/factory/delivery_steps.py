from base64 import b64decode
from functools import partial
import json
from pathlib import Path

from factory.evidence import Conflict, Evidence, digest, encode, timestamp
from factory.fixture import validate_receipt
from factory.local_rpc import call


def require(condition, message: str):
    if not condition:
        raise Conflict(message)


def check_verdict(verdict: dict, *, base: str, head: str, checks: bytes,
                  bundle_digest: str, decision: str, reason: str):
    require(verdict["schema_version"] == 1, "unsupported verifier schema")
    require(verdict["base_sha"] == base, "verifier base mismatch")
    require(verdict["checked_sha"] == verdict["expected_head"] == head, "verifier head mismatch")
    require(verdict["check_digest"] == digest(checks), "verifier check digest mismatch")
    require(verdict["bundle_sha256"] == bundle_digest, "verifier bundle digest mismatch")
    require(verdict["decision"] == decision and verdict["reason"] == reason, "unexpected verdict")
    cases = json.loads(checks)["cases"]
    require(len(verdict["checks"]) == len(cases), "verifier check count mismatch")
    failures = []
    for case, result in zip(cases, verdict["checks"], strict=True):
        require(result["id"] == case["id"], "verifier check ID mismatch")
        outputs = {}
        for channel in ("stdout", "stderr"):
            output = result[channel]
            outputs[channel] = b64decode(output["base64"], validate=True)
            require(output["sha256"] == digest(outputs[channel]), "verifier output hash mismatch")
        passed = result["exit_code"] == 0 and (
            outputs == {"stdout": case["stdout"].encode(), "stderr": b""}
        )
        require(result["passed"] is passed, "verifier result mismatch")
        if not passed:
            failures.append(result["id"])
    observed_reason = f"check_failed:{failures[0]}" if failures else "checks_passed"
    require(reason == observed_reason, "verifier results disagree with verdict")


class Delivery:
    def __init__(self, store: Evidence, checks: bytes, environment: str, rpc_root: Path):
        self.store = store
        self.checks = checks
        self.environment = environment
        self.candidate = partial(call, rpc_root / "candidate" / "service.sock")
        self.verifier = partial(call, rpc_root / "verifier" / "service.sock")
        self.fixture = partial(call, rpc_root / "fixture" / "service.sock")

    def verify(self, snapshot: dict, step: str, decision: str, reason: str) -> dict:
        base, head = snapshot["base_sha"], snapshot["head_sha"]
        result = self.verifier(
            "verify", base=base, head=head, bundle=snapshot["bundle"], check_digest=digest(self.checks),
        )
        bundle = self.store.blob(b64decode(snapshot["bundle"], validate=True))
        verdict = result["verdict"].encode()
        check_verdict(
            json.loads(verdict), base=base, head=head, checks=self.checks,
            bundle_digest=bundle, decision=decision, reason=reason,
        )
        require(result["exit_code"] == int(decision == "rejected"), "unexpected verifier exit")
        return {
            "base_sha": base, "head_sha": head,
            "checkpoint": self.save_checkpoint(snapshot, step),
            "verdict": self.store.blob(verdict), "bundle": bundle,
        }

    def save_checkpoint(self, snapshot: dict, step: str) -> str:
        return self.store.blob(encode({
            **snapshot, "schema_version": 1, "last_completed_step": step,
            "environment_digest": self.environment,
        }))

    def prepare(self, request: dict) -> dict:
        run_id = request["run_id"]
        with self.store.lock(run_id):
            require(request["environment_digest"] == self.environment, "environment digest changed")
            require(self.store.read(run_id, "request") == request, "run request changed")
            require(request["schema_version"] == 1 and request["fixture"] == "records", "unsupported request")
            red = self.store.read(run_id, "red")
            if red is None:
                snapshot = self.candidate("start", run_id=run_id)
                require(snapshot["base_sha"] == request["base_sha"], "fixture base changed")
                red = self.verify(snapshot, "red", "rejected", "check_failed:final_record_without_newline")
                self.store.record(run_id, "red", red)
            green = self.store.read(run_id, "green")
            if green is None:
                snapshot = self.candidate(
                    "improve", run_id=run_id, red=json.loads(self.store.read_blob(red["checkpoint"])),
                )
                green = self.verify(snapshot, "green", "accepted", "checks_passed")
                self.store.record(run_id, "green", green)
            prepared = self.store.read(run_id, "prepared")
            if prepared is None:
                snapshot = json.loads(self.store.read_blob(green["checkpoint"]))
                prepared = self.store.record(run_id, "prepared", {
                    "schema_version": 1, "request_digest": digest(encode(request)),
                    "environment_digest": self.environment, "base_sha": green["base_sha"],
                    "submitted_sha": green["head_sha"], "verdict": "accepted",
                    "artifacts": {
                        "checks": self.store.blob(self.checks),
                        "red_checkpoint": red["checkpoint"], "red_verdict": red["verdict"],
                        "red_bundle": red["bundle"], "green_checkpoint": green["checkpoint"],
                        "submission_checkpoint": self.save_checkpoint(snapshot, "submission"),
                        "verifier_verdict": green["verdict"], "submitted_bundle": green["bundle"],
                    },
                })
            return prepared

    def deliver(self, request: dict, prepared: dict, execution: dict, after_apply) -> dict:
        run_id = request["run_id"]
        with self.store.lock(run_id):
            require(request["environment_digest"] == self.environment, "environment digest changed")
            require(self.store.read(run_id, "request") == request, "run request changed")
            require(self.store.read(run_id, "prepared") == prepared, "prepared submission changed")
            finished = self.store.read(run_id, "manifest")
            if finished is not None:
                require(finished["temporal_run_id"] == execution["temporal_run_id"], "execution changed")
                return reopen(self.store, run_id)
            artifacts = dict(prepared["artifacts"])
            selected = self.store.latest_checkpoint(
                artifacts["submission_checkpoint"], artifacts["green_checkpoint"],
            )
            missing = {
                name: artifacts.pop(name)
                for name in ("submission_checkpoint", "green_checkpoint")
                if not self.store.path(artifacts[name]).exists()
            }
            check_submissions(prepared, {name: self.store.read_blob(ref) for name, ref in artifacts.items()})
            if selected is None:
                recovery = {"status": "interrupted", "reason": "missing_verified_checkpoint"}
                if self.store.read(run_id, "intent") is not None:
                    self.reconcile(run_id, prepared, artifacts, after_apply, allow_apply=False)
            else:
                step, data = selected
                snapshot = json.loads(data)
                require(snapshot["schema_version"] == 1, "unsupported checkpoint schema")
                require(snapshot["last_completed_step"] == step, "checkpoint step mismatch")
                require(snapshot["environment_digest"] == self.environment, "checkpoint environment changed")
                require(snapshot["head_sha"] == prepared["submitted_sha"], "checkpoint head mismatch")
                require(snapshot["base_sha"] == prepared["base_sha"], "checkpoint base mismatch")
                require(digest(b64decode(snapshot["bundle"], validate=True)) == artifacts["submitted_bundle"],
                        "checkpoint bundle differs from the verified submission")
                restored_tree = self.candidate("restore", run_id=run_id, snapshot=snapshot)
                require(restored_tree == snapshot["tree"], "candidate restored a different tree")
                recovery = {
                    "status": "restored" if execution["delivery_attempt"] > 1 or step == "green" else "initial",
                    "checkpoint_step": step, "checkpoint_digest": digest(data), "tree": restored_tree,
                }
                self.store.record(run_id, f"restore-{execution['delivery_attempt']}", recovery)
                self.reconcile(run_id, prepared, artifacts, after_apply)

            manifest = {
                **prepared, **execution, "artifacts": artifacts, "missing_artifacts": missing,
                "run_id": run_id, "producer": "records-delivery", "completed_at": timestamp(),
                "status": "interrupted" if selected is None else "delivered",
                "recovery": recovery, "model_calls": 0,
            }
            check_manifest(self.store, manifest)
            return self.store.record(run_id, "manifest", manifest)

    def reconcile(self, run_id: str, prepared: dict, artifacts: dict, after_apply, *, allow_apply=True):
        operation_id = f"records-delivery:{run_id}"
        effect = {
            "schema_version": 1, "operation_id": operation_id,
            "submitted_sha": prepared["submitted_sha"], "verifier_verdict": artifacts["verifier_verdict"],
        }
        request_digest = digest(encode(effect))
        intent = {**effect, "request_digest": request_digest}
        self.store.record(run_id, "intent", intent)
        artifacts["intent"] = self.store.blob(encode(intent))
        retained = self.store.read(run_id, "receipt")
        receipt = self.fixture("lookup", operation_id=operation_id)
        if retained is not None:
            require(receipt == retained, "fixture receipt differs from retained receipt")
        needs_apply = receipt is None and allow_apply
        if needs_apply:
            receipt = self.fixture("apply", operation_id=operation_id, request_digest=request_digest)
        if receipt is not None:
            validate_receipt(receipt, operation_id, request_digest)
            if needs_apply:
                after_apply()
            self.store.record(run_id, "receipt", receipt)
            artifacts["receipt"] = self.store.blob(encode(receipt))


def check_submissions(manifest: dict, blobs: dict[str, bytes]):
    for prefix, head, decision, reason in (
        ("red", manifest["base_sha"], "rejected", "check_failed:final_record_without_newline"),
        ("submitted", manifest["submitted_sha"], "accepted", "checks_passed"),
    ):
        verdict_name = "red_verdict" if prefix == "red" else "verifier_verdict"
        check_verdict(
            json.loads(blobs[verdict_name]), base=manifest["base_sha"], head=head, checks=blobs["checks"],
            bundle_digest=digest(blobs[f"{prefix}_bundle"]), decision=decision, reason=reason,
        )


def check_manifest(store: Evidence, manifest: dict) -> dict[str, bytes]:
    require(manifest["schema_version"] == 1, "unsupported manifest schema")
    require(manifest["verdict"] == "accepted", "invalid submitted verdict")
    request = store.read(manifest["run_id"], "request", required=True)
    require(digest(encode(request)) == manifest["request_digest"], "retained request digest differs")
    artifacts = manifest["artifacts"]
    blobs = {name: store.read_blob(reference) for name, reference in artifacts.items()}
    check_submissions(manifest, blobs)
    recovery = manifest["recovery"]
    if manifest["status"] == "delivered":
        checkpoint_name = f"{recovery['checkpoint_step']}_checkpoint"
        require(artifacts[checkpoint_name] == recovery["checkpoint_digest"], "recovery checkpoint differs")
        snapshot = json.loads(blobs[checkpoint_name])
        require(snapshot["tree"] == recovery["tree"], "recovered tree differs")
        require("receipt" in blobs, "delivered run is missing a receipt")
    else:
        require(manifest["status"] == recovery["status"] == "interrupted", "invalid terminal status")
        require({"submission_checkpoint", "green_checkpoint"} <= manifest["missing_artifacts"].keys(),
                "interrupted run has a verified checkpoint")

    if "intent" in blobs or "receipt" in blobs:
        intent = json.loads(blobs["intent"])
        require(store.read(manifest["run_id"], "intent", required=True) == intent, "retained intent differs")
        request_digest = intent.pop("request_digest")
        require(intent["operation_id"] == f"records-delivery:{manifest['run_id']}", "operation ID differs")
        require(intent["submitted_sha"] == manifest["submitted_sha"], "effect head differs")
        require(intent["verifier_verdict"] == artifacts["verifier_verdict"], "effect verdict differs")
        require(request_digest == digest(encode(intent)), "effect request digest differs")
        if "receipt" in blobs:
            receipt = json.loads(blobs["receipt"])
            require(store.read(manifest["run_id"], "receipt", required=True) == receipt, "retained receipt differs")
            validate_receipt(receipt, intent["operation_id"], request_digest)
    return blobs


def reopen(store: Evidence, run_id: str) -> dict:
    manifest = store.read(run_id, "manifest")
    require(manifest is not None and manifest["run_id"] == run_id, "terminal manifest missing")
    check_manifest(store, manifest)
    return manifest
