from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


TASK_QUEUE = "records-delivery"


@workflow.defn
class RecordsDelivery:
    @workflow.run
    async def run(self, request: dict) -> dict:
        retry = RetryPolicy(maximum_attempts=3)
        prepared = await workflow.execute_activity(
            "prepare_records", request, start_to_close_timeout=timedelta(seconds=90),
            retry_policy=retry,
        )
        return await workflow.execute_activity(
            "deliver_records", args=(request, prepared), start_to_close_timeout=timedelta(seconds=25),
            retry_policy=retry,
        )
