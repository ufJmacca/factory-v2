import json
import math
import re
import time

from factory.evidence import Conflict, digest, encode, validate_run_id


ACTIONS = ("push", "pull_request", "ci_status", "deploy")


def product_marker(request):
    return request.get("predecessor", request)["run_id"]


def branch(request):
    return "codex/product/" + product_marker(request)


def observation_start(pr):
    return pr.get("head_observation_epoch", pr["created_epoch"])


def pull_request_payload(request):
    return {
        "head": branch(request), "base": request["base"], "title": request.get("title", f"Product delivery {request['run_id']}"),
        "body": (f"Verified product revision: `{request['revision']}`\n\n"
                 f"<!-- factory-run:{product_marker(request)} -->"),
    }


def ci_payload(request, ci):
    return {
        "state": "success" if ci["exit_code"] == 0 else "failure",
        "context": request["ci_publish"]["name"],
        "description": f"Factory verified patch suite ({request['run_id']})",
    }


def validate_request(request):
    validate_run_id(request["run_id"])
    if (
        request["schema_version"] != 1
        or not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", request["repo"])
        or not re.fullmatch(r"[0-9a-f]{40}", request["revision"])
        or request["base"] != "main"
        or not all(re.fullmatch(r"[A-Za-z0-9-]+", request[key]) for key in ("approver", "reviewer"))
        or request["approver"].lower() == request["reviewer"].lower()
        or request.get("review_source", "github") not in {"github", "frozen_grader"}
        or (request.get("ci_publish") is not None and request["ci_publish"] not in request["checks"])
        or (request.get("ci_publish") is not None and request["ci_publish"]["kind"] != "status")
        or not math.isfinite(request["deadline"])
        or type(request["observation_seconds"]) is not int
        or request["observation_seconds"] <= 0
        or (request["environment"] is not None and not re.fullmatch(r"[a-z0-9-]+", request["environment"]))
        or not request["checks"]
        or any(set(check) != {"kind", "name", "actor"} or check["kind"] not in {"check_run", "status"}
               or not check["name"] or not check["actor"] for check in request["checks"])
        or len({(check["kind"], check["name"]) for check in request["checks"]}) != len(request["checks"])
    ):
        raise Conflict("invalid product delivery policy")
    if previous := request.get("predecessor"):
        if (validate_run_id(previous["run_id"]) == request["run_id"]
            or previous["revision"] == request["revision"]
            or not all(re.fullmatch(r"[0-9a-f]{40}", previous[key]) for key in ("revision", "base_sha"))
            or request["environment"] is not None):
            raise Conflict("invalid product integration policy")


def scope(request, action):
    return {
        "run_id": request["run_id"], "actor": request["approver"], "action": action, "repo": request["repo"],
        "revision": request["revision"], "environment": request["environment"] if action == "deploy" else None,
    }


def record_approval(store, run_id, grant):
    if grant["action"] not in ACTIONS or grant["run_id"] != run_id or not math.isfinite(grant["expires_at"]):
        raise Conflict("invalid approval record")
    ref = digest(encode(grant))
    return store.record(run_id, f"approval-{grant['action']}-{ref}", grant)


def grants(store, request, action, now):
    run_id = request["run_id"]
    for path in sorted(store.run_dir(run_id).glob(f"approval-{action}-*.json")):
        grant = store.read(run_id, path.stem)
        if grant is not None and valid_grant(request, action, grant, now):
            yield path.stem, grant


def valid_grant(request, action, grant, now):
    return math.isfinite(grant["expires_at"]) and now < grant["expires_at"] and all(
        grant.get(key) == value for key, value in scope(request, action).items()
    )


def deployment_target(request):
    return {key: request[key] for key in ("repo", "revision", "environment")}


class FixtureDeployment:
    def __init__(self, store):
        self.store = store

    def find(self, request):
        target = deployment_target(request)
        key = "d-" + digest(encode(target))[:60]
        return self.store.read(key, "deployment")

    def deploy(self, request):
        target = deployment_target(request)
        key = "d-" + digest(encode(target))[:60]
        self.store.record(key, "deployment", {**target, "id": key, "provider": "fixture"})


def intent_for(request, action, ci=None):
    targets = {
        "push": {"ref": "refs/heads/" + branch(request),
                 "previous_revision": request.get("predecessor", {}).get("revision")},
        "pull_request": pull_request_payload(request),
        "deploy": {"environment": request["environment"], "provider": "fixture"},
    }
    target = (
        {"publication": ci_payload(request, ci), "ci_receipt": digest(encode(ci))}
        if action == "ci_status" else targets[action]
    )
    if action == "push" and target["previous_revision"] is None:
        del target["previous_revision"]
    operation = {**target, "run_id": request["run_id"], "repo": request["repo"],
                 "revision": request["revision"], "action": action, "policy_digest": digest(encode(request))}
    request_digest = digest(encode(operation))
    return {"schema_version": 1, "operation_id": f"{action}-{request_digest}",
            "request_digest": request_digest, "request": operation}


def matches(request, action, result, ci=None):
    if action == "ci_status":
        return result.get("actor") == request["ci_publish"]["actor"] and result.get("revision") == request["revision"] and all(
            result.get(key) == value for key, value in ci_payload(request, ci).items()
        )
    expected = {
        "push": {"ref": "refs/heads/" + branch(request), "revision": request["revision"]},
        "pull_request": {"head_ref": branch(request), "base": request["base"], "marker": product_marker(request),
                         "repo": request["repo"], "revision": request["revision"]},
        "deploy": {**deployment_target(request), "provider": "fixture",
                   "id": "d-" + digest(encode(deployment_target(request)))[:60]},
    }[action]
    return all(result.get(key) == value for key, value in expected.items())


def gate(request, snapshot):
    if snapshot["revision"] != request["revision"]:
        return "head_changed"
    if snapshot["state"] != "open":
        return "pull_request_closed"
    for required in request["checks"]:
        if not any(
            all(check.get(key) == value for key, value in required.items())
            and check["revision"] == request["revision"] and check["state"] == "success"
            for check in snapshot["checks"]
        ):
            return "awaiting_checks"
    review = snapshot["review"]
    if not review or (review["actor"], review["revision"], review["state"]) != (
        request["reviewer"], request["revision"], "approved"
    ):
        return "awaiting_review"
    return None


def verify_ci(store, request, ci):
    if ci["revision"] != request["revision"] or ci["check"] != request["ci_publish"]:
        raise Conflict("CI receipt differs from the product head or required check")
    store.read_blob(ci["log"])


def frozen_review(store, request, result):
    source = request["source"]
    ref = result["verdict"]
    verdict = json.loads(store.read_blob(ref))
    retained_verdict = json.loads(store.read_blob(source["independent_verdict"]))
    if (
        verdict["checked_sha"] != request["revision"]
        or verdict["grader_digest"] != retained_verdict["grader_digest"]
        or result["bundle"] != source["integrated_bundle" if "predecessor" in request else "submitted_bundle"]
        or result["reason"] != verdict["decision"]
        or not verdict["checks"]
    ):
        raise Conflict("frozen review differs from the product head, bundle, or approved grader")
    passed = verdict["decision"] == "accepted" and all(check["passed"] for check in verdict["checks"])
    return {"actor": request["reviewer"], "revision": request["revision"],
            "state": "approved" if passed else "changes_requested", "id": ref}


def verify_submission(store, request):
    verdict_key = "integration_verdict" if "predecessor" in request else "independent_verdict"
    verdict = json.loads(store.read_blob(request["source"][verdict_key]))
    if (verdict["checked_sha"], verdict["decision"]) != (request["revision"], "accepted"):
        raise Conflict("independent verdict does not accept this product head")


def record_observation(store, run_id, observation):
    minutes = observation.get("active_human_minutes")
    if (
        not math.isfinite(observation["observed_at"])
        or type(observation["feedback"]) is not str
        or (minutes is not None and (type(minutes) not in (int, float)
                                    or not math.isfinite(minutes) or minutes < 0))
    ):
        raise Conflict("invalid operator observation")
    return store.record(run_id, "operator-" + digest(encode(observation)), observation)


def operator_observations(request, records, pr):
    if not pr:
        return []
    start = observation_start(pr)
    return sorted(
        (item for item in records
         if item["actor"] == request["approver"] and item["revision"] == request["revision"]
         and item["pull_request"] == pr["number"]
         and start <= item["observed_at"] <= start + request["observation_seconds"]),
        key=lambda item: (item["observed_at"], digest(encode(item))),
    )


def prior_pull_request(store, request):
    previous = request["predecessor"]
    report = json.loads(store.read_blob(request["source"]["prior_product_manifest"]))
    pr = report["pull_request"]
    if (
        report["run_id"] != previous["run_id"] or report["status"] != "observed"
        or report["accepted_revision"] != previous["revision"] or pr["revision"] != previous["revision"]
        or pr["repo"] != request["repo"] or pr["base"] != request["base"]
        or pr["marker"] != product_marker(request) or pr["head_ref"] != branch(request)
    ):
        raise Conflict("prior product report does not authorize reuse of this pull request")
    return pr


def same_pull_request(pr, previous):
    return all(pr[key] == previous[key] for key in ("number", "url", "created_epoch"))


def matches_prior(store, request, pr):
    return matches(request, "pull_request", pr) and same_pull_request(pr, prior_pull_request(store, request))


class ProductRunner:
    def __init__(self, store, remote, deployment=None, *, clock=time.time):
        self.store = store
        self.remote = remote
        self.deployment = deployment
        self.clock = clock

    def effect(self, request, action, lookup, write, *, precondition=lambda: True, ci=None):
        store = self.store
        run_id = request["run_id"]
        if receipt := store.read(run_id, "receipt-" + action):
            return receipt["remote"]
        intent = intent_for(request, action, ci)
        attempt = store.read(run_id, "attempt-" + action)
        result = lookup(request)
        receipt_source = "reconciled"
        if result is not None:
            if not matches(request, action, result, ci):
                raise Conflict(f"conflicting remote {action}")
            if attempt is None and action != "deploy":
                raise Conflict(f"remote {action} exists without a recorded attempt")
        elif attempt is not None:
            raise Conflict(f"ambiguous {action} has no matching remote result")
        if attempt is None:
            now = self.clock()
            if now >= request["deadline"] or next(grants(store, request, action, now), None) is None:
                return None
            store.record(run_id, "intent-" + action, intent)
            if not precondition():
                return None
            now = self.clock()
            grant = next(grants(store, request, action, now), None)
            if grant is None or now >= request["deadline"]:
                return None
            name, _ = grant
            attempt = store.record(run_id, "attempt-" + action, {
                "operation_id": intent["operation_id"], "approval": name, "executed_at": now,
            })
            if result is None:
                try:
                    write(request)
                    receipt_source = "confirmed"
                except OSError:
                    pass
                result = lookup(request)
                if result is None or not matches(request, action, result, ci):
                    raise Conflict(f"{action} has no matching remote result after dispatch")
        store.record(run_id, "receipt-" + action, {
            "operation_id": intent["operation_id"], "request_digest": intent["request_digest"],
            "receipt_source": receipt_source, "remote": result,
        })
        return result

    def advance(self, run_id):
        store = self.store
        with store.lock(run_id):
            if store.read(run_id, "product-manifest"):
                return reopen_product(store, run_id)
            request = store.read(run_id, "request")
            validate_request(request)
            verify_submission(store, request)
            try:
                return self.advance_admitted(request)
            except Conflict as error:
                return finish(store, request, self.clock(), "conflict", detail=str(error))

    def advance_admitted(self, request):
        store = self.store
        run_id = request["run_id"]

        def pending(reason):
            if self.clock() >= request["deadline"]:
                return finish(store, request, self.clock(), "deadline", detail=reason)
            return {"status": reason}

        def expected_push(req):
            remote_ref = self.remote.find_push(req)
            if "predecessor" in request:
                if remote_ref is None:
                    raise Conflict("product ref disappeared before the integration update")
                if remote_ref["revision"] == request["predecessor"]["revision"]:
                    return None
            return remote_ref

        def integration_pr_is_open():
            if "predecessor" in request:
                prior = prior_pull_request(store, request)
                current = self.remote.find_pr(request)
                if (not current or not same_pull_request(current, prior) or current["state"] != "open"
                    or current["revision"] != request["predecessor"]["revision"]):
                    raise Conflict("the original product PR changed before the integration push")
            return True

        if "predecessor" in request:
            prior_pull_request(store, request)
        if not self.effect(request, "push", expected_push, self.remote.push, precondition=integration_pr_is_open):
            return pending("awaiting_push_approval")

        def current_ref():
            ref = self.remote.find_push(request)
            if ref is None or not matches(request, "push", ref):
                raise Conflict("product ref changed before PR creation")
            return True

        if "predecessor" in request:
            pr = store.read(run_id, "adopted-pull_request")
            if pr is None:
                current = self.remote.find_pr(request)
                if current and current["revision"] == request["predecessor"]["revision"]:
                    return pending("awaiting_integrated_pr_head")
                if not current or current["state"] != "open" or not matches_prior(store, request, current):
                    raise Conflict("existing product PR does not have the authorized integration head")
                pr = store.record(run_id, "adopted-pull_request", {
                    **current, "head_observation_epoch": self.clock(),
                })
        else:
            pr = self.effect(request, "pull_request", self.remote.find_pr,
                             self.remote.create_pr, precondition=current_ref)
        if not pr:
            return pending("awaiting_pull_request_approval")

        def inspect_pr():
            snapshot = self.remote.inspect(request, pr)
            if request.get("review_source") == "frozen_grader":
                review = store.read(run_id, "frozen-review")
                snapshot["review"] = (
                    frozen_review(store, request, review) if review and snapshot["revision"] == request["revision"]
                    else None
                )
            snapshot.update(pull_request=pr["number"], repo=request["repo"], observed_at=self.clock())
            store.record(run_id, "snapshot-" + digest(encode(snapshot)), snapshot)
            return snapshot

        snapshot = inspect_pr()
        reason = gate(request, snapshot)
        if reason in {"head_changed", "pull_request_closed"}:
            return finish(store, request, self.clock(), reason, snapshot)
        if request.get("ci_publish") and not store.read(run_id, "receipt-ci_status"):
            ci = store.read(run_id, "ci-preflight")
            if ci is None:
                return pending("awaiting_ci_receipt")
            verify_ci(store, request, ci)
            def current_open_pr():
                current = self.remote.find_pr(request)
                if (not current or not same_pull_request(current, pr) or current["state"] != "open"
                    or not matches(request, "pull_request", current)):
                    raise Conflict("product PR changed before CI status publication")
                return current_ref()

            if not self.effect(request, "ci_status", lambda req: self.remote.find_status(req, ci),
                               lambda req: self.remote.publish_status(req, ci), precondition=current_open_pr, ci=ci):
                return pending("awaiting_ci_status_approval")
            snapshot = inspect_pr()
            reason = gate(request, snapshot)
            if reason in {"head_changed", "pull_request_closed"}:
                return finish(store, request, self.clock(), reason, snapshot)
        if reason:
            return pending(reason)
        if request["environment"]:
            if self.deployment is None:
                raise Conflict("fixture deployment is unavailable")

            def check_deployment_head():
                if gate(request, inspect_pr()) is not None:
                    raise Conflict("product evidence changed before fixture deployment")
                return current_ref()

            if not self.effect(request, "deploy", self.deployment.find, self.deployment.deploy,
                               precondition=check_deployment_head):
                return pending("awaiting_deploy_approval")
        now = self.clock()
        if now < observation_start(pr) + request["observation_seconds"]:
            return pending("observing")
        if now > request["deadline"]:
            return pending("observation_window_expired")
        return finish(store, request, now, "observed", snapshot)


def pull_request_from(records):
    return records.get("adopted-pull_request") or records.get("receipt-pull_request", {}).get("remote")


def observation_summary(request, records, pr, snapshot):
    observations = operator_observations(
        request, (value for name, value in records.items() if name.startswith("operator-")), pr,
    )
    minutes = [entry.get("active_human_minutes") for entry in observations
               if entry.get("active_human_minutes") is not None]
    return {
        "observation_window": [observation_start(pr), observation_start(pr) + request["observation_seconds"]]
        if pr else None,
        "feedback": [*(snapshot.get("feedback", []) if snapshot else []), *observations] or "unknown",
        "active_human_minutes": minutes[-1] if minutes else "unknown",
        "corrections": snapshot["corrections"] if snapshot is not None else "unknown",
    }


def finish(store, request, now, reason, snapshot=None, *, detail=None):
    run_id = request["run_id"]
    records = {path.stem: store.read(run_id, path.stem) for path in store.run_dir(run_id).glob("*.json")
               if path.stem.startswith(("approval-", "intent-", "attempt-", "receipt-", "snapshot-", "operator-"))
               or path.stem in {"ci-preflight", "frozen-review", "dispatch-product", "adopted-pull_request"}}
    pr = pull_request_from(records)
    deployment = records.get("receipt-deploy", {}).get("remote")
    if snapshot is None:
        snapshots = [value for name, value in records.items() if name.startswith("snapshot-")]
        snapshot = max(snapshots, key=lambda item: item["observed_at"], default=None)
    return store.record(run_id, "product-manifest", {
        "schema_version": 1, "producer": "deliver-product", "run_id": run_id, "completed_at_epoch": now,
        "status": "observed" if reason == "observed" else "unshipped", "reason": reason, "detail": detail,
        "request_digest": digest(encode(request)), "pull_request": pr, "deployment": deployment,
        "accepted_revision": request["revision"] if reason == "observed" else None,
        "snapshot": "snapshot-" + digest(encode(snapshot)) if snapshot is not None else None,
        **observation_summary(request, records, pr, snapshot),
        "execution": records.get("dispatch-product"),
        "artifacts": {name: store.blob(encode(value)) for name, value in records.items()},
        "source": request["source"],
    })


def reopen_product(store, run_id):
    report = store.read(run_id, "product-manifest")
    request = store.read(run_id, "request")
    if not report or report["run_id"] != run_id or report["request_digest"] != digest(encode(request)):
        raise Conflict("product manifest differs from the admitted request")
    validate_request(request)
    verify_submission(store, request)
    if report["source"] != request["source"]:
        raise Conflict("product source evidence differs")
    for ref in report["source"].values():
        store.read_blob(ref)
    if "verification_recovery" in report["source"]:
        from factory.product_source import load_candidate
        load_candidate(store, request)
    records = {name: json.loads(store.read_blob(ref)) for name, ref in report["artifacts"].items()}
    ci = records.get("ci-preflight")
    if ci is not None:
        verify_ci(store, request, ci)
    review = records.get("frozen-review")
    if review is not None:
        review = frozen_review(store, request, review)
    snapshot = records.get(report["snapshot"])
    if report["execution"] != records.get("dispatch-product"):
        raise Conflict("product workflow execution differs")
    if snapshot is not None and (
        report["pull_request"] is None or snapshot["pull_request"] != report["pull_request"]["number"]
        or snapshot["repo"] != request["repo"]
        or report["corrections"] != snapshot["corrections"]
    ):
        raise Conflict("product evidence belongs to a different pull request")
    for action in ACTIONS:
        receipt = records.get("receipt-" + action)
        attempt = records.get("attempt-" + action)
        if attempt is not None:
            intent = intent_for(request, action, ci)
            if (records["intent-" + action] != intent or attempt["operation_id"] != intent["operation_id"]
                or attempt["executed_at"] >= request["deadline"]
                or not valid_grant(request, action, records[attempt["approval"]], attempt["executed_at"])):
                raise Conflict(f"{action} was executed without matching authorization")
        if receipt is not None and (
            attempt is None or receipt["operation_id"] != attempt["operation_id"]
            or receipt["request_digest"] != intent["request_digest"]
            or not matches(request, action, receipt["remote"], ci)
        ):
            raise Conflict(f"{action} receipt differs from the authorized target")
    pr = pull_request_from(records)
    adopted = records.get("adopted-pull_request")
    if adopted is not None and (
        "predecessor" not in request or not matches_prior(store, request, pr)
        or records.get("receipt-pull_request") is not None
    ):
        raise Conflict("unbound previously opened product pull request")
    if report["pull_request"] != pr or report["deployment"] != records.get("receipt-deploy", {}).get("remote"):
        raise Conflict("unbound pull request or deployment in product report")
    if any(report[key] != value for key, value in observation_summary(request, records, pr, snapshot).items()):
        raise Conflict("product observation summary differs from retained feedback")
    if report["status"] == "observed":
        if (
            report["accepted_revision"] != request["revision"]
            or records.get("receipt-push") is None or report["pull_request"] is None
            or snapshot is None or gate(request, snapshot) is not None
            or (request.get("review_source") == "frozen_grader" and snapshot["review"] != review)
            or (request.get("ci_publish") is not None and records.get("receipt-ci_status") is None)
            or report["completed_at_epoch"] < report["observation_window"][1]
            or report["completed_at_epoch"] > request["deadline"]
            or bool(request["environment"]) != bool(report["deployment"])
        ):
            raise Conflict("product report has unverified acceptance")
    elif report["accepted_revision"] is not None:
        raise Conflict("unshipped product report claims an accepted revision")
    return report
