"""Verify Temporal indexes the receipt published after runner cleanup."""
import asyncio
import json
import os
from pathlib import Path
from time import time

from temporalio.client import Client

from factory.delivery import OUTCOME_QUEUE, OutcomeDelivery
from factory.evidence import Evidence, encode


def admit(store, run_id):
    return store.admit({
        "run_id": run_id,
        "deadline_epoch": time() + 4,
        "max_attempts": 2,
        "configuration": store.blob(encode({"source_digest": store.blob(b"profile"), "skills": {}})),
        "base_checkpoint": store.blob(b"checkpoint"),
        "grader_digest": store.blob(b"grader"),
    })


async def check_deadline(client, store, run_id, runner_reason):
    request = admit(store, run_id)
    handle = await client.start_workflow(
        OutcomeDelivery.run, request, id=f"deliver-outcome/{run_id}", task_queue=OUTCOME_QUEUE,
    )
    while store.read(run_id, "dispatch-attempt-1") is None:
        if time() >= request["deadline_epoch"]:
            raise AssertionError("fixture attempt was not dispatched before the deadline")
        await asyncio.sleep(0.05)

    await asyncio.sleep(max(0, request["deadline_epoch"] + 1.5 - time()))
    assert store.read(run_id, "manifest") is None, "workflow finalized before runner cleanup published the receipt"
    usage = {"input_tokens": 13, "output_tokens": 3}
    events = encode({"type": "turn.completed", "usage": usage})
    receipt = store.record(run_id, "attempt-1", {
        "attempt": 1, "reason": runner_reason, "usage": usage, "native_events": store.blob(events),
    })

    result = await asyncio.wait_for(handle.result(), timeout=12)
    assert result["reason"] == "deadline_exceeded", result
    assert result["status"] == "incomplete", result
    assert json.loads(store.read_blob(result["artifacts"]["attempt-1"])) == receipt
    assert store.read_blob(result["artifacts"]["attempt-1_events"]) == events
    assert store.read(run_id, "dispatch-attempt-2") is None
    assert store.read(run_id, "dispatch-verify") is None
    print(f"{run_id}: indexed runner usage and events after deadline without dispatching more work")


async def check_missing_receipt(client, store):
    run_id = "missing-runner-receipt"
    request = admit(store, run_id)
    handle = await client.start_workflow(
        OutcomeDelivery.run, request, id=f"deliver-outcome/{run_id}", task_queue=OUTCOME_QUEUE,
    )
    result = await asyncio.wait_for(handle.result(), timeout=48)
    assert result["reason"] == "receipt_timeout", result
    assert result["status"] == "incomplete", result
    assert store.read(run_id, "dispatch-attempt-1") is not None
    assert store.read(run_id, "dispatch-attempt-2") is None
    print(f"{run_id}: finalized explicitly after bounded receipt wait")


async def main():
    client = await Client.connect(os.environ["TEMPORAL_ADDRESS"])
    store = Evidence(Path(os.environ["OUTCOME_EVIDENCE"]))
    await asyncio.gather(
        check_deadline(client, store, "deadline-attempt", "deadline_exceeded"),
        check_deadline(client, store, "late-submission-receipt", "completed"),
        check_deadline(client, store, "late-interruption-receipt", "interrupted"),
        check_missing_receipt(client, store),
    )


if __name__ == "__main__":
    asyncio.run(main())
