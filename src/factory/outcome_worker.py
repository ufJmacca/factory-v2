import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys

from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.worker import Worker

from factory.codex_evidence import reopen
from factory.delivery import EVALUATION_QUEUE, OUTCOME_QUEUE, OutcomeDelivery, ProductDelivery
from factory.delivery_steps import require
from factory.evidence import Evidence, digest, encode, timestamp
from factory.harbor_receipt import apply_fixture
from factory.product import finish as finish_product, reopen_product, validate_request, verify_submission


def action_key(ticket):
    return f"attempt-{ticket['attempt']}" if ticket["kind"] == "attempt" else "verify"


class OutcomeActivities:
    def __init__(self, store: Evidence):
        self.store = store

    def run_id(self, request):
        run_id = request["run_id"]
        if self.store.read(run_id, "request") != request:
            raise ApplicationError("approved request changed", non_retryable=True)
        return run_id

    @activity.defn(name="queue_codex_action")
    def queue(self, request, ticket):
        run_id = self.run_id(request)
        self.store.record(run_id, "dispatch-" + action_key(ticket), ticket)

    @activity.defn(name="read_codex_result")
    def read(self, request, ticket):
        return self.store.read(self.run_id(request), action_key(ticket))

    @activity.defn(name="apply_harbor_fixture")
    def apply_harbor_fixture(self, request, verification):
        self.run_id(request)
        return apply_fixture(self.store, request, verification)

    @activity.defn(name="finish_codex_delivery")
    def finish(self, request, execution):
        run_id = self.run_id(request)
        if self.store.read(run_id, "manifest") is not None:
            return reopen(self.store, run_id)
        artifacts = {
            "configuration": request["configuration"], "base_checkpoint": request["base_checkpoint"],
            "grader": request["grader_digest"],
        }
        attempts = [f"attempt-{n}{suffix}" for n in range(1, request["max_attempts"] + 1)
                    for suffix in ("", "-intent", "-workspace", "-marker", "-workspace-proof")]
        for name in ("investigation", "red", "green", "refactor", "submission", *attempts):
            if (record := self.store.read(run_id, name)) is not None:
                artifacts[name] = self.store.blob(encode(record))
                for field in ("test", "checkpoint", "native_events"):
                    if field in record:
                        artifacts[f"{name}_{'events' if field == 'native_events' else field}"] = record[field]
        verified = execution["reason"] == "accepted"
        verdict = self.store.read(run_id, "verify")
        if verdict and "verdict" in verdict:
            artifacts.update(independent_verdict=verdict["verdict"], submitted_bundle=verdict["bundle"])
        if request.get("evaluation"):
            for name in ("harbor-export", "harbor-grade", "harbor-fixture", "provider-start", "provider-stop"):
                if (record := self.store.read(run_id, name)) is not None:
                    artifacts[name] = self.store.blob(encode(record))
            if verified:
                require(all(name in artifacts for name in ("harbor-export", "harbor-grade", "harbor-fixture",
                                                           "provider-start", "provider-stop")),
                        "Harbor delivery is missing its grade, fixture or provider evidence")
        missing = {}
        if execution["reason"] == "pinned_configuration_unavailable":
            for name, ref in list(artifacts.items()):
                try:
                    self.store.read_blob(ref)
                except (FileNotFoundError, Conflict):
                    missing[name] = artifacts.pop(name)
        submission = self.store.read(run_id, "submission")
        manifest = {
            **execution, "schema_version": 1, "run_id": run_id, "producer": "deliver-outcome",
            "request_digest": digest(encode(request)), "artifacts": artifacts, "completed_at": timestamp(),
            "status": "verified" if verified else "incomplete", "missing_artifacts": missing,
            "submitted_sha": submission["head_sha"] if submission else None,
        }
        if request.get("transport", {}).get("kind") == "cli-proxy":
            artifacts["proxy_ca"] = request["transport"]["ca_digest"]
        self.store.record(run_id, "manifest", manifest)
        return reopen(self.store, run_id)


class ProductActivities(OutcomeActivities):
    @activity.defn(name="dispatch_product")
    def dispatch(self, request, context):
        run_id = self.run_id(request)
        validate_request(request)
        verify_submission(self.store, request)
        self.store.record(run_id, "dispatch-product", context)

    @activity.defn(name="read_product")
    def read_product(self, request):
        run_id = self.run_id(request)
        if self.store.read(run_id, "product-manifest"):
            return reopen_product(self.store, run_id)

    @activity.defn(name="expire_product")
    def expire(self, request):
        run_id = self.run_id(request)
        with self.store.lock(run_id):
            if result := self.read_product(request):
                return result
            return finish_product(self.store, request, activity.info().current_attempt_scheduled_time.timestamp(),
                                  "deadline", detail="no terminal runner receipt before the deadline")


async def submit(client, store, run_id, *, product=False):
    request = store.read(run_id, "request")
    require(request is not None, "admitted request missing")
    if product:
        validate_request(request)
        verify_submission(store, request)
    definition = ProductDelivery.run if product else OutcomeDelivery.run
    workflow_id = f"deliver-{'product' if product else 'outcome'}/{run_id}"
    try:
        return await client.start_workflow(
            definition, request, id=workflow_id,
            task_queue=EVALUATION_QUEUE if request.get("evaluation") else OUTCOME_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id)
        started = await anext(handle.fetch_history_events(page_size=1))
        payloads = started.workflow_execution_started_event_attributes.input.payloads
        require(await client.data_converter.decode(payloads) == [request], "existing workflow request differs")
        return handle


async def main(mode, run_id=None):
    client = await Client.connect(os.environ["TEMPORAL_ADDRESS"])
    store = Evidence(Path(os.environ.get("OUTCOME_EVIDENCE", "/evidence")))
    if mode == "wait-product":
        result = await client.get_workflow_handle(f"deliver-product/{run_id}").result()
        require(result == reopen_product(store, run_id), "workflow result differs from the retained product evidence")
        store.record(run_id, "temporal-completion", {
            "execution": result["execution"], "manifest_digest": digest(encode(result)),
        })
        return
    if mode in {"submit", "submit-product"}:
        await submit(client, store, run_id, product=mode == "submit-product")
        return
    activities = OutcomeActivities(store)
    product = ProductActivities(store)
    queue = os.environ.get("OUTCOME_TASK_QUEUE", OUTCOME_QUEUE)
    slots = 2 if queue == EVALUATION_QUEUE else 1
    with ThreadPoolExecutor(max_workers=slots) as executor:
        await Worker(
            client, workflows=[OutcomeDelivery, ProductDelivery], task_queue=queue,
            activities=[activities.queue, activities.read, activities.finish, activities.apply_harbor_fixture,
                        product.dispatch,
                        product.read_product, product.expire],
            activity_executor=executor, max_concurrent_activities=slots,
        ).run()


if __name__ == "__main__":
    if sys.argv[1] == "reopen":
        print(json.dumps(reopen(Evidence(Path(sys.argv[2])), sys.argv[3], *sys.argv[4:]), sort_keys=True))
    else:
        asyncio.run(main(*sys.argv[1:]))
