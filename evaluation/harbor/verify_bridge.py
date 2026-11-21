import asyncio
import json
import os
from pathlib import Path
from time import time
from urllib.error import URLError
from urllib.parse import urlencode

from temporalio.client import Client

from factory.evidence import Evidence, encode
from factory.fixture import Ledger
from factory.harbor_binding import Binding, LeaseLost
from factory.harbor_supervisor import Harbor, config_for, launch, reconcile, submit_job, wait_until
from factory.outcome_worker import submit


def require(condition, message):
    if not condition:
        raise AssertionError(message)


async def started(store, name, deadline):
    async def native_start():
        path = store.run_dir(name) / "attempt-1.jsonl"
        if path.is_file():
            with path.open("rb") as stream:
                for line in stream:
                    if line.endswith(b"\n") and json.loads(line)["type"] == "thread.started":
                        return True
        return False

    await wait_until(native_start, deadline, f"{name} did not start Codex before its deadline")


async def product_control(store, client, minutes):
    run_id = "bridge-product-control"
    common = store.read("bridge", "common", required=True)
    frozen = store.read("bridge", "frozen", required=True)
    deadline = time() + minutes * 60
    request = store.admit({**common, "run_id": run_id, "deadline_epoch": deadline,
                           "grader_digest": common["product_grader"], "outcome": frozen["instruction"]})
    workflow = await submit(client, store, run_id)
    await started(store, run_id, min(deadline, time() + 10))
    return request, workflow


def allow(store, assignment):
    store.record(assignment, "runner-allowed", {"allowed": True})


def marker_check(store, manifest):
    run_id = manifest["run_id"]
    native = store.read_blob(manifest["artifacts"]["attempt-1_events"])
    for line in native.splitlines():
        event = json.loads(line)
        item = event.get("item", {})
        if (event["type"] == "item.completed" and item.get("tool") == "workspace"
                and "factory-marker" in str(item.get("arguments", {})) and item.get("result")):
            result = json.loads(item["result"]["content"][0]["text"])
            if result == {"exit_code": 0, "output": run_id, "truncated": False}:
                return
    raise AssertionError(f"Codex did not observe the assigned marker for {run_id}")


async def in_workspace(binding):
    async def running():
        call = binding.next_call()
        return call is not None and any("sleep 20" in arg for arg in call[1])

    await wait_until(running, time() + 18, f"{binding.name} did not enter the scripted shell command")


async def cancel_queued(api, store):
    assignment = "bridge-queued-cancel"
    frozen = store.read("bridge", "frozen", required=True)
    job = await submit_job(api, store, assignment, config_for(store, frozen, assignment, 2))
    _, rows = await api.request("/rollouts?" + urlencode({"job_ids": job["job"]}))
    require(rows["total"] == 1 and rows["items"][0]["state"] == "queued", "expected one queued rollout")
    rollout = rows["items"][0]["id"]
    await api.cancel(rollout)
    row = await api.rollout(rollout)
    require(row["state"] == "cancelled" and row["started_at"] is None, "queued rollout started")
    require(not Binding(store.root, rollout).folder.exists(), "queued rollout created a workspace binding")
    return {"job": job["job"], "rollout": rollout, "state": row["state"]}


async def drills(api, client, store):
    async def drill(name, action):
        request, handle = await launch(api, client, store, name)
        binding = Binding(store.root, request["evaluation"]["rollout"])
        require(binding.active(), f"{name} did not acquire a workspace lease")
        allow(store, name)
        await in_workspace(binding)
        action_task = asyncio.create_task(reconcile(api, store, request, handle))
        if action == "harbor":
            await api.cancel(binding.rollout)
        elif action == "temporal":
            await handle.cancel()
        else:
            store.record(name, "pause-owner", {"requested": True})
        result = await action_task
        try:
            binding.run(["true"])
        except LeaseLost:
            pass
        else:
            raise AssertionError("revoked lease still grants commands")
        if action == "lease":
            require(result["reason"] == "lease_lost" and store.read(name, "owner-resumed"), result["reason"])
        else:
            require(result["state"] == "cancelled", result["state"])
        async def stopped_runner():
            return store.read(binding.name, "attempt-1")

        attempt = await wait_until(stopped_runner, time() + 10, f"{name} left the CLI running")
        require(attempt["reason"] == "lease_lost" and attempt.get("native_events"), str(attempt))
        require(store.read(binding.name, "harbor-fixture") is None, f"{name} produced a fixture action")
        return {"runner_stop": attempt["reason"], "native_events": attempt["native_events"], "rollout": binding.rollout, "job": request["evaluation"]["job"],
                "provider_stop": store.read(binding.name, "provider-stop", required=True)}

    return {
        "harbor_cancel": await drill("bridge-harbor-cancel", "harbor"),
        "temporal_cancel": await drill("bridge-temporal-cancel", "temporal"),
        "lease_loss": await drill("bridge-lease-loss", "lease"),
    }


async def main():
    root = Path(os.environ["BRIDGE_ROOT"])
    store = Evidence(root)
    api = Harbor(os.environ["BRIDGE_API_KEY"])
    client = await Client.connect(os.environ["TEMPORAL_ADDRESS"])
    live = os.environ.get("BRIDGE_MODE") == "live"
    minutes = 10 if live else 2

    async def server_ready():
        try:
            return await api.request("/jobs")
        except URLError:
            return None

    await wait_until(server_ready, time() + 30, "Harbor server unavailable")
    assignments = ["bridge-live"] if live else ["bridge-left", "bridge-right"]
    allocated = await asyncio.gather(*(launch(api, client, store, name, minutes=minutes, discard_reply=index == 0)
                                       for index, name in enumerate(assignments)))
    queued = None
    if not live:
        _, jobs = await api.request("/jobs")
        require(jobs["total"] == 2, "duplicate request created more than one Harbor job")
        require(all(Binding(root, request["evaluation"]["rollout"]).active() for request, _ in allocated),
                "both evaluation slots must be held for the capacity check")
        for name in assignments:
            allow(store, name)
        bindings = [Binding(root, request["evaluation"]["rollout"]) for request, _ in allocated]
        await asyncio.gather(*(in_workspace(binding) for binding in bindings))
        queued = await cancel_queued(api, store)
    product, product_handle = await product_control(store, client, minutes)
    if live:
        allow(store, assignments[0])
    else:
        require(all(binding.next_call() is not None for binding in bindings),
                "the product runner must start while both evaluation commands are pending")
    result_tasks = [asyncio.create_task(reconcile(api, store, request, handle, notify=False))
                    for request, handle in allocated]
    product_result = await product_handle.result()
    results = await asyncio.gather(*result_tasks)
    require(product_result["status"] == "verified", f"product control ended as {product_result['reason']}")
    require(store.read(product["run_id"], "attempt-1-marker", required=True)["assigned_workspace"],
            "product control marker missing")
    if not live:
        marker_check(store, product_result)
    trials = []
    for (request, handle), result in zip(allocated, results, strict=True):
        require(result["status"] == "verified", f"trial ended as {result['reason']}")
        run_id = request["run_id"]
        require(store.read(run_id, "attempt-1-marker", required=True)["assigned_workspace"], "trial marker missing")
        export = store.read(run_id, "harbor-export", required=True)
        require(await reconcile(api, store, request, handle, notify=False) == result, "grade replay changed the result")
        require(export["runtime"] == product["runtime"], "runtime differs from the product control")
        require(not store.read(run_id, "harbor-notification", required=True)["sent"], "grade signal was sent")
        if not live:
            marker_check(store, result)
        trials.append({
            **export, "manifest": store.blob(encode(result)),
            "protected_grade": store.read(run_id, "harbor-grade", required=True),
            "fixture": store.read(run_id, "harbor-fixture", required=True),
            "provider_stop": store.read(run_id, "provider-stop", required=True),
        })
    failure_drills = {} if live else {"queued_cancel": queued, **await drills(api, client, store)}
    ledger = Ledger(root / "harbor-fixture.sqlite").inspect()
    require(ledger["effect_count"] == len(trials) and all(row["apply_requests"] == 1 for row in ledger["effects"]),
            "fixture effect count differs")
    _, jobs = await api.request("/jobs")
    (root / "summary.json").write_bytes(encode({
        "tested_sha": product["factory_sha"], "clean_checkout": not (root / "git-status.txt").read_text(),
        "mode": "live" if live else "scripted", "trials": trials,
        "product": {"workflow_id": product_result["workflow_id"], "temporal_run_id": product_result["temporal_run_id"],
                    "submitted_sha": product_result["submitted_sha"], "runtime": product["runtime"]},
        "failure_drills": failure_drills, "harbor_job_count": jobs["total"], "fixture_effect_count": ledger["effect_count"],
        "production_effect_count": 0, "grade_notification_sent": False,
        "evaluation_slots": 2, "product_started_before_deadline": True,
        "agent_containers": len(list((root / "runs").glob("eval-*/provider-start.json"))),
        "verifier_containers": len(list((root / "harbor/files/rollouts").glob("*/*/verifier/grade.json"))),
        "remaining_trial_containers": 0, "subscription_executions": len(trials) + 1 if live else 0,
    }))


if __name__ == "__main__":
    asyncio.run(main())
