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

from factory.checkpoint import decode
from factory.delivery import OUTCOME_QUEUE, OutcomeDelivery
from factory.delivery_steps import require
from factory.evidence import Evidence, digest, encode, timestamp


def action_key(ticket):
    return f"attempt-{ticket['attempt']}" if ticket["kind"] == "attempt" else "verify"


def reopen(store, run_id):
    manifest = store.read(run_id, "manifest")
    require(manifest is not None and manifest["run_id"] == run_id, "terminal manifest missing")
    request = store.read(run_id, "request")
    require(digest(encode(request)) == manifest["request_digest"], "request digest differs")
    artifacts = {name: store.read_blob(ref) for name, ref in manifest["artifacts"].items()}
    profile = json.loads(artifacts["configuration"])
    store.read_blob(profile["source_digest"])
    for reference in profile["skills"].values():
        store.read_blob(reference)
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

    @activity.defn(name="finish_codex_delivery")
    def finish(self, request, execution):
        run_id = self.run_id(request)
        if self.store.read(run_id, "manifest") is not None:
            return reopen(self.store, run_id)
        artifacts = {
            "configuration": request["configuration"], "base_checkpoint": request["base_checkpoint"],
            "grader": request["grader_digest"],
        }
        for name in ("investigation", "red", "green", "refactor", "submission",
                     *[f"attempt-{n}" for n in range(1, request["max_attempts"] + 1)]):
            if (record := self.store.read(run_id, name)) is not None:
                artifacts[name] = self.store.blob(encode(record))
                for field in ("test", "checkpoint", "native_events"):
                    if field in record:
                        artifacts[f"{name}_{'events' if field == 'native_events' else field}"] = record[field]
        verified = execution["reason"] == "accepted"
        verdict = self.store.read(run_id, "verify")
        if verdict:
            artifacts.update(independent_verdict=verdict["verdict"], submitted_bundle=verdict["bundle"])
        submission = self.store.read(run_id, "submission")
        manifest = {
            **execution, "schema_version": 1, "run_id": run_id, "producer": "deliver-outcome",
            "request_digest": digest(encode(request)), "artifacts": artifacts, "completed_at": timestamp(),
            "status": "verified" if verified else "incomplete",
            "submitted_sha": submission["head_sha"] if submission else None,
        }
        self.store.record(run_id, "manifest", manifest)
        return reopen(self.store, run_id)


async def main(mode, run_id=None):
    client = await Client.connect(os.environ["TEMPORAL_ADDRESS"])
    store = Evidence(Path(os.environ.get("OUTCOME_EVIDENCE", "/evidence")))
    if mode == "submit":
        request = store.read(run_id, "request")
        try:
            await client.start_workflow(
                OutcomeDelivery.run, request, id=f"deliver-outcome/{run_id}", task_queue=OUTCOME_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            handle = client.get_workflow_handle(f"deliver-outcome/{run_id}")
            started = await anext(handle.fetch_history_events(page_size=1))
            payloads = started.workflow_execution_started_event_attributes.input.payloads
            require(await client.data_converter.decode(payloads) == [request], "existing workflow request differs")
        return
    activities = OutcomeActivities(store)
    with ThreadPoolExecutor(max_workers=1) as executor:
        await Worker(
            client, workflows=[OutcomeDelivery], task_queue=OUTCOME_QUEUE,
            activities=[activities.queue, activities.read, activities.finish],
            activity_executor=executor, max_concurrent_activities=1,
        ).run()


if __name__ == "__main__":
    if sys.argv[1] == "reopen":
        print(json.dumps(reopen(Evidence(Path(sys.argv[2])), sys.argv[3]), sort_keys=True))
    else:
        asyncio.run(main(*sys.argv[1:]))
