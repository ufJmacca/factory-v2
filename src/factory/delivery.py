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


OUTCOME_QUEUE = "codex-delivery"


@workflow.defn(name="deliver-outcome")
class OutcomeDelivery:
    @workflow.run
    async def run(self, request: dict) -> dict:
        context = {"workflow_id": workflow.info().workflow_id, "temporal_run_id": workflow.info().run_id}

        async def exchange(operation, payload):
            return await workflow.execute_activity(
                operation, args=(request, payload), start_to_close_timeout=timedelta(seconds=15),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

        async def wait_for(ticket):
            await exchange("queue_codex_action", ticket)
            while workflow.time() < request["deadline_epoch"]:
                if (result := await exchange("read_codex_result", ticket)) is not None:
                    return result
                await workflow.sleep(min(2, max(0, request["deadline_epoch"] - workflow.time())))
            return {"reason": "deadline_exceeded"}

        reason = "attempts_exhausted"
        for attempt in range(1, request["max_attempts"] + 1):
            receipt = await wait_for({"kind": "attempt", "attempt": attempt})
            reason = receipt["reason"]
            if reason == "completed":
                receipt = await wait_for({"kind": "verify"})
                reason = receipt["reason"]
                break
            if reason != "interrupted":
                break
        return await exchange("finish_codex_delivery", {**context, "reason": reason})
