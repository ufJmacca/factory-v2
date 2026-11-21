import asyncio
import json
from time import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError

from factory.evidence import Conflict, digest, encode, validate_run_id
from factory.harbor_binding import Binding
from factory.harbor_receipt import reconcile_grade
from factory.outcome_worker import submit


class Harbor:
    def __init__(self, token):
        self.token = token

    def exchange(self, path, payload=None, key=None):
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        request = Request("http://harbor-bridge:8390" + path, None if payload is None else encode(payload),
                          headers=headers)
        try:
            response = urlopen(request, timeout=20)
        except HTTPError as error:
            response = error
        with response:
            data = response.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                raise Conflict("oversized Harbor response")
            return response.code, data

    async def request(self, path, payload=None, *, key=None, status=(200,)):
        code, data = await asyncio.to_thread(self.exchange, path, payload, key)
        if code not in status:
            raise Conflict(f"Harbor {path.partition('?')[0]} returned {code}: {data[:1200].decode()}")
        return code, json.loads(data)

    async def download(self, uri):
        status, data = await asyncio.to_thread(self.exchange, "/download?" + urlencode({"uri": uri}))
        if status != 200:
            raise Conflict(f"Harbor artifact download returned {status}")
        return data

    async def rollout(self, name):
        return (await self.request(f"/rollouts/{name}"))[1]["rollout"]

    async def cancel(self, name):
        return await self.request(f"/rollouts/{name}/cancel", {"force": True})


def config_for(store, frozen, assignment, minutes):
    output = store.root / "publish" / validate_run_id(assignment)
    output.mkdir(parents=True, exist_ok=True)
    empty = store.root / "empty-agent-logs"
    empty.mkdir(mode=0o555, exist_ok=True)
    mounts = [{"type": "bind", "source": str(empty), "target": target, "read_only": True,
               "bind": {"create_host_path": False}} for target in ("/logs/verifier", "/logs/artifacts")]
    mounts.append({"type": "bind", "source": str(output), "target": "/output",
                   "bind": {"create_host_path": False}})
    return {"config": {
        "job_name": assignment,
        "tasks": [{"path": frozen["task"]}],
        "agents": [{"import_path": "evaluation.harbor.factory_agent:FactoryAgent", "kwargs": {"assignment": assignment}}],
        "environment": {"type": "docker", "cpu_enforcement_policy": "limit", "memory_enforcement_policy": "limit",
                        "kwargs": {"auto_mounting": True}, "mounts": mounts},
        "n_concurrent_trials": 1, "retry": {"max_retries": 0},
        "agent_timeout_multiplier": (minutes * 60 + 65) / 45,
    }}


async def submit_job(api, store, name, config, *, discard_reply=False):
    request_digest = digest(encode(config))
    store.record(name, "harbor-intent", {"key": name, "request_digest": request_digest})
    if discard_reply:
        await api.request("/jobs/submit", config, key=name)
    code, reply = await api.request("/jobs/submit", config, key=name, status=(200, 409))
    if discard_reply and code != 409:
        raise Conflict("the duplicate Harbor request did not return its existing job")
    job = reply["job_id"]
    if code == 409:
        store.record(name, "harbor-duplicate", {"job": job, "status": code})
    return store.record(name, "harbor-job", {"job": job, "request_digest": request_digest})


async def wait_until(read, deadline, description):
    while time() < deadline:
        if result := await read():
            return result
        await asyncio.sleep(0.2)
    raise TimeoutError(description)


async def launch(api, client, store, name, *, minutes=2, discard_reply=False):
    frozen = store.read("bridge", "frozen", required=True)
    common = store.read("bridge", "common", required=True)
    config = config_for(store, frozen, name, minutes)
    assignment = store.read(name, "assignment")
    if assignment is None:
        assignment = store.record(name, "assignment", {
            "instruction": frozen["instruction"], "base_sha": frozen["base_sha"],
            "deadline_epoch": time() + minutes * 60,
        })
    deadline = assignment["deadline_epoch"]
    _, dry = await api.request("/jobs/submit", {**config, "dry_run": True})
    if dry != {"n_trials": 1}:
        raise Conflict("unexpected Harbor dry-run plan")
    store.record(name, "harbor-dry-run", dry)
    receipt = await submit_job(api, store, name, config, discard_reply=discard_reply)
    _, listing = await api.request("/rollouts?" + urlencode({"job_ids": receipt["job"]}))
    if listing["total"] != 1 or len(listing["items"]) != 1:
        raise Conflict("expected exactly one rollout")
    _, job = await api.request(f"/jobs/{receipt['job']}")
    lock_bytes = await api.download(job["job"]["lock_uri"])
    lock = json.loads(lock_bytes)
    if len(lock["trials"]) != 1:
        raise Conflict("Harbor job lock differs from the dry run")
    rollout = listing["items"][0]["id"]
    binding = Binding(store.root, rollout)
    evaluation = {key: frozen[key] for key in ("task_checksum", "task_digest", "grader_digest", "harbor_revision")}
    request = store.admit({
        **common, "run_id": binding.name, "outcome": frozen["instruction"], "deadline_epoch": deadline,
        "workspace_image": frozen["workspace_image"],
        "grader_digest": frozen["grader_digest"],
        "evaluation": {**evaluation, "job": receipt["job"], "rollout": rollout,
                       "job_lock": store.blob(lock_bytes), "harbor_task_lock": lock["trials"][0]["task"]["digest"],
                       "grade_deadline_epoch": deadline + 100},
    })
    async def ready():
        return store.read(binding.name, "harbor-ready")

    proof = await wait_until(ready, deadline, "FactoryAgent did not open its workspace")
    if proof["assignment"] != name or proof["rollout"] != rollout:
        raise Conflict("Harbor agent opened the wrong rollout")
    handle = await submit(client, store, binding.name)
    execution = await handle.describe()
    store.record(name, "dispatch", {"rollout": rollout, "job": receipt["job"], "run_id": binding.name,
                                    "temporal_run_id": execution.run_id})
    return request, handle


async def reconcile(api, store, request, handle, *, notify=True):
    run_id = request["run_id"]
    spec = request["evaluation"]
    binding = Binding(store.root, spec["rollout"])
    deadline = spec["grade_deadline_epoch"]
    while time() < deadline:
        row = await api.rollout(binding.rollout)
        workflow = await handle.describe()
        if row["state"] in ("cancelling", "cancelled"):
            binding.revoke("harbor_cancelled")
            if workflow.status == WorkflowExecutionStatus.RUNNING:
                await handle.cancel()
        elif workflow.status == WorkflowExecutionStatus.CANCELED:
            await api.cancel(binding.rollout)
            binding.revoke("temporal_cancelled")
        stopped = store.read(run_id, "provider-stop")
        if row["state"] == "cancelled" and workflow.status == WorkflowExecutionStatus.CANCELED and stopped:
            return row
        if row["state"] == "finished" and stopped:
            export = store.read(run_id, "harbor-export")
            if workflow.status == WorkflowExecutionStatus.CANCELED:
                return row
            if export and (row["result"] or {}).get("verifier_result"):
                paths = row["artifacts"]
                bundle = await api.download(paths["artifacts/output/submission.bundle"])
                grade = json.loads(await api.download(paths["verifier/grade.json"]))
                reconcile_grade(store, request, export, row, grade, bundle)
                if store.read(run_id, "harbor-notification") is None:
                    sent = notify and workflow.status == WorkflowExecutionStatus.RUNNING
                    if sent:
                        try:
                            await handle.signal("harbor_grade_available")
                        except RPCError:
                            sent = False
                    store.record(run_id, "harbor-notification", {"sent": sent})
                return await handle.result()
            if store.read(run_id, "dispatch-verify"):
                reason = "harbor_grade_missing" if export else "harbor_export_missing"
                store.record(run_id, "verify", {"reason": reason})
            if workflow.status != WorkflowExecutionStatus.RUNNING:
                return await handle.result()
        await asyncio.sleep(0.25)
    binding.revoke("reconciliation_timeout")
    raise TimeoutError(f"Harbor reconciliation timed out for {run_id}")
