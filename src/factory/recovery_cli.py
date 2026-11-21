import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import re

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from factory.delivery import RecordsDelivery, TASK_QUEUE
from factory.evidence import Conflict, Evidence, digest, encode, validate_run_id
from factory.local_rpc import call


RECORDS_BASE = "9fd2fa086e38341550a01211784a085eccedd7b9"


async def run(args):
    if args.command == "fixture":
        return call(Path("/rpc/fixture/service.sock"), "inspect")
    run_id = validate_run_id(args.run_id)
    store = Evidence(Path("/evidence"))
    workflow_id = f"records-delivery/{run_id}"
    if args.command == "submit":
        if not re.fullmatch(r"[0-9a-f]{40}", args.base):
            raise ValueError("base must be a full Git SHA")
        request = store.admit({
            "schema_version": 1, "run_id": run_id, "fixture": "records", "base_sha": args.base,
            "environment_digest": os.environ["RECOVERY_ENVIRONMENT_DIGEST"],
        })
        original_execution = store.read(run_id, "execution")
        if original_execution is not None:
            return original_execution
    client = await Client.connect(os.environ["TEMPORAL_ADDRESS"])
    if args.command == "submit":
        try:
            handle = await client.start_workflow(
                RecordsDelivery.run, request, id=workflow_id, task_queue=TASK_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                execution_timeout=timedelta(minutes=5),
            )
        except WorkflowAlreadyStartedError:
            handle = client.get_workflow_handle(workflow_id)
            started = await anext(handle.fetch_history_events(page_size=1))
            payloads = started.workflow_execution_started_event_attributes.input.payloads
            if await client.data_converter.decode(payloads) != [request]:
                raise Conflict("existing Temporal workflow has a different request")
        execution = await handle.describe()
        return store.record(run_id, "execution", {
            "run_id": run_id, "workflow_id": workflow_id, "temporal_run_id": execution.run_id,
            "request_digest": digest(encode(request)),
        })
    handle = client.get_workflow_handle(workflow_id)
    if args.command == "wait":
        return await asyncio.wait_for(handle.result(), timeout=100)
    execution = await handle.describe()
    pending = execution.raw_description.pending_activities
    return {
        "status": execution.status.name, "workflow_id": workflow_id, "temporal_run_id": execution.run_id,
        "pending_activities": [
            {"name": item.activity_type.name, "state": item.state, "attempt": item.attempt}
            for item in pending
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("run_id")
    submit.add_argument("--base", default=RECORDS_BASE)
    for name in ("wait", "status"):
        commands.add_parser(name).add_argument("run_id")
    commands.add_parser("fixture")
    try:
        print(json.dumps(asyncio.run(run(parser.parse_args())), sort_keys=True))
    except (Conflict, ValueError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
