import asyncio
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
EVALUATION_QUEUE = "harbor-delivery"
RECEIPT_DRAIN_SECONDS = 30


@workflow.defn(name="deliver-outcome")
class OutcomeDelivery:
    def __init__(self):
        self.grade_available = False

    @workflow.signal
    def harbor_grade_available(self):
        self.grade_available = True

    @workflow.run
    async def run(self, request: dict) -> dict:
        context = {"workflow_id": workflow.info().workflow_id, "temporal_run_id": workflow.info().run_id}
        execution_deadline = request["deadline_epoch"]
        receipt_deadline = execution_deadline + RECEIPT_DRAIN_SECONDS

        async def exchange(operation, payload):
            return await workflow.execute_activity(
                operation, args=(request, payload), start_to_close_timeout=timedelta(seconds=15),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

        async def wait_for(ticket):
            evaluation_grade = request.get("evaluation") if ticket["kind"] == "verify" else None
            deadline = evaluation_grade["grade_deadline_epoch"] if evaluation_grade else receipt_deadline
            if workflow.time() >= (deadline if evaluation_grade else execution_deadline):
                return {"reason": "deadline_exceeded"}
            await exchange("queue_codex_action", ticket)
            while True:
                if (result := await exchange("read_codex_result", ticket)) is not None:
                    return result
                remaining = deadline - workflow.time()
                if remaining <= 0:
                    return {"reason": "receipt_timeout"}
                if evaluation_grade:
                    try:
                        await workflow.wait_condition(lambda: self.grade_available, timeout=min(2, remaining))
                    except asyncio.TimeoutError:
                        pass
                    self.grade_available = False
                else:
                    await workflow.sleep(min(2, remaining))

        reason = "attempts_exhausted"
        for attempt in range(1, request["max_attempts"] + 1):
            receipt = await wait_for({"kind": "attempt", "attempt": attempt})
            reason = receipt["reason"]
            if reason == "completed":
                ticket = {"kind": "verify"}
                if request.get("evaluation"):
                    ticket.update(context)
                receipt = await wait_for(ticket)
                reason = receipt["reason"]
                if reason == "accepted" and request.get("evaluation"):
                    await exchange("apply_harbor_fixture", receipt)
                break
            if reason != "interrupted":
                break
        return await exchange("finish_codex_delivery", {**context, "reason": reason})


@workflow.defn(name="deliver-product")
class ProductDelivery:
    @workflow.run
    async def run(self, request: dict) -> dict:
        context = {"workflow_id": workflow.info().workflow_id, "temporal_run_id": workflow.info().run_id}

        async def exchange(operation, *args):
            return await workflow.execute_activity(
                operation, args=(request, *args), start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

        await exchange("dispatch_product", context)
        while True:
            if result := await exchange("read_product"):
                return result
            remaining = request["deadline"] - workflow.time()
            if remaining <= 0:
                return await exchange("expire_product")
            await workflow.sleep(min(2, remaining))
