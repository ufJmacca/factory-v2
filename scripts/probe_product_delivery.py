import asyncio
import json
import os
from pathlib import Path
import sys
from time import time

from temporalio.client import Client

from factory.evidence import Evidence
from factory.outcome_worker import submit
from factory.product import ProductRunner, reopen_product

sys.path.insert(0, "/tmp")
from product_probe_support import ProductFixture, grant, request_for


class LostResponse(ProductFixture):
    def find_pr(self, request):
        if self.pr and self.lose_pr:
            self.lose_pr = False
            raise OSError("the first reconciliation read lost its response")
        return super().find_pr(request)

    def create_pr(self, request):
        try:
            super().create_pr(request)
        finally:
            if self.pr:
                self.pr["created_epoch"] = time()


async def dispatched(store, run_id):
    async with asyncio.timeout(8):
        while store.read(run_id, "dispatch-product") is None:
            await asyncio.sleep(0.05)


async def lost_reply(client, store):
    run_id = "product-lost-reply"
    request = request_for(store, run_id=run_id)
    request.update(deadline=time() + 15, observation_seconds=1)
    store.admit(request)
    remote = LostResponse(store)
    main = await submit(client, store, run_id, product=True)
    duplicate = await submit(client, store, run_id, product=True)
    assert main.first_execution_run_id is not None
    await dispatched(store, run_id)
    runner = ProductRunner(store, remote)
    assert runner.advance(run_id)["status"] == "awaiting_push_approval"
    for action in ("push", "pull_request"):
        grant(store, request, action, expires_at=request["deadline"])
    remote.lose_pr = True
    try:
        runner.advance(run_id)
    except OSError:
        assert store.read(run_id, "receipt-pull_request") is None
    else:
        raise AssertionError("fixture did not lose the successful PR creation response")
    assert remote.writes == ["push", "pull_request"]
    remote.verify()
    restarted = ProductRunner(store, remote)
    await asyncio.sleep(1.1)
    closed = restarted.advance(run_id)
    assert closed["status"] == "observed", closed
    async with asyncio.timeout(8):
        assert await main.result() == await duplicate.result() == reopen_product(store, run_id)
    receipt = store.read(run_id, "receipt-pull_request")
    assert (receipt["receipt_source"], receipt["remote"]["number"]) == ("reconciled", 4)
    assert remote.writes == ["push", "pull_request"]
    store.record(run_id, "provider-write-counts", {action: remote.writes.count(action) for action in remote.writes})
    print("Duplicate Temporal submissions and a lost PR creation response produced one PR.")


async def withheld(client, store):
    run_id = "product-withheld"
    request = request_for(store, run_id=run_id)
    request.update(deadline=time() + 5)
    store.admit(request)
    handle = await submit(client, store, run_id, product=True)
    await dispatched(store, run_id)
    remote = ProductFixture(store)
    assert ProductRunner(store, remote).advance(run_id)["status"] == "awaiting_push_approval"
    async with asyncio.timeout(10):
        result = await handle.result()
    assert (result["status"], result["reason"], result["pull_request"]) == ("unshipped", "deadline", None)
    assert remote.writes == []
    print("Temporal closed the withheld run unshipped at the deadline.")


async def main():
    store = Evidence(Path(os.environ["OUTCOME_EVIDENCE"]))
    client = await Client.connect(os.environ["TEMPORAL_ADDRESS"])
    await asyncio.gather(lost_reply(client, store), withheld(client, store))


if __name__ == "__main__":
    asyncio.run(main())
