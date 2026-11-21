import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import signal

from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from factory.delivery import RecordsDelivery, TASK_QUEUE
from factory.delivery_steps import Delivery
from factory.evidence import Evidence


class Activities:
    def __init__(self, delivery: Delivery, crash_run: str | None):
        self.delivery = delivery
        self.crash_run = crash_run

    @activity.defn(name="prepare_records")
    def prepare(self, request: dict) -> dict:
        try:
            return self.delivery.prepare(request)
        except (ValueError, KeyError, FileNotFoundError) as error:
            raise ApplicationError(str(error), non_retryable=True) from error

    @activity.defn(name="deliver_records")
    def deliver(self, request: dict, prepared: dict) -> dict:
        info = activity.info()

        def after_apply():
            if request["run_id"] == self.crash_run:
                os.kill(os.getpid(), signal.SIGKILL)

        execution = {
            "workflow_id": info.workflow_id, "temporal_run_id": info.workflow_run_id,
            "delivery_attempt": info.attempt,
        }
        try:
            return self.delivery.deliver(request, prepared, execution, after_apply)
        except (ValueError, KeyError, FileNotFoundError) as error:
            raise ApplicationError(str(error), non_retryable=True) from error


async def main():
    delivery = Delivery(
        Evidence(Path("/evidence")), Path("/grading/checks.json").read_bytes(),
        os.environ["RECOVERY_ENVIRONMENT_DIGEST"], Path("/rpc"),
    )
    activities = Activities(delivery, os.environ.get("RECOVERY_CRASH_RUN"))
    client = await Client.connect(os.environ["TEMPORAL_ADDRESS"])
    with ThreadPoolExecutor(max_workers=4) as executor:
        await Worker(
            client, task_queue=TASK_QUEUE, workflows=[RecordsDelivery],
            activities=[activities.prepare, activities.deliver],
            activity_executor=executor, max_concurrent_activities=4,
        ).run()


if __name__ == "__main__":
    asyncio.run(main())
