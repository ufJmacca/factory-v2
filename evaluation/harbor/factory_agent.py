import asyncio
from pathlib import Path
import os
import shlex
from time import time

from harbor.agents.base import BaseAgent
from harbor.models.trajectories.trajectory import Trajectory

from factory.evidence import Conflict, Evidence
from factory.harbor_binding import Binding, LeaseLost


class FactoryAgent(BaseAgent):
    def __init__(self, *, assignment, **kwargs):
        super().__init__(**kwargs)
        self.assignment = assignment

    @staticmethod
    def name():
        return "factory-delivery"

    def version(self):
        return "1"

    async def setup(self, environment):
        pass

    async def run(self, instruction, environment, context):
        store = Evidence(Path(os.environ["FACTORY_BRIDGE_ROOT"]))
        assignment = store.read(self.assignment, "assignment", required=True)
        if instruction.strip() != assignment["instruction"].strip():
            raise Conflict("the Harbor instruction differs from the locked assignment")
        binding = Binding(store.root, str(self.context_id))
        probe = await environment.exec(
            command="test \"$(id -u)\" = 10001 && test ! -e /var/run/docker.sock && "
                    f"test \"$(git rev-parse HEAD)\" = {shlex.quote(assignment['base_sha'])} && hostname",
            cwd="/work/product", user="10001:10001", timeout_sec=10,
        )
        if probe.return_code:
            raise Conflict("Harbor workspace identity probe failed")
        binding.open()
        reason = "agent_failed"
        try:
            store.record(binding.name, "harbor-ready", {
                "rollout": binding.rollout, "assignment": self.assignment, "container": probe.stdout.strip(),
            })
            await self.work(environment, context, store, binding, assignment["deadline_epoch"])
            reason = "submitted"
        except asyncio.CancelledError:
            reason = "harbor_cancelled"
            raise
        finally:
            binding.revoke(reason)

    async def work(self, environment, context, store, binding, deadline):
        pending = None
        call_id = None
        try:
            while time() <= deadline + 20:
                binding.pulse()
                if pending is not None and pending.done():
                    binding.reply(call_id, pending.result())
                    pending = None
                if pending is None:
                    if export := store.read(binding.name, "harbor-export"):
                        await self.export(environment, context, store, export)
                        return
                    if attempt := store.read(binding.name, "attempt-1"):
                        if attempt["reason"] != "completed":
                            raise Conflict(f"delivery attempt ended with {attempt['reason']}")
                    if call := binding.next_call():
                        call_id, argv, limit = call
                        pending = asyncio.create_task(self.execute(environment, binding, argv, limit))
                await asyncio.sleep(0.08)
            raise LeaseLost("Harbor assignment deadline")
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    @staticmethod
    async def execute(environment, binding, argv, limit):
        if not 0 < limit <= 16 * 1024 * 1024:
            raise Conflict("invalid workspace output limit")
        command = shlex.join(["timeout", "-k", "1s", "45s", *argv])
        drain = f"{{ head -c {limit + 1}; cat >/dev/null; }}"
        wrapped = "bash -o pipefail -c " + shlex.quote(f"{command} 2>&1 | {drain}")
        binding.require_active()
        result = await environment.exec(command=wrapped, cwd="/work/product",
                                        user="10001:10001", timeout_sec=50)
        data = ((result.stdout or "") + (result.stderr or "")).encode()
        return {"exit_code": result.return_code, "output": data[:limit].decode(errors="replace"),
                "truncated": len(data) > limit}

    @staticmethod
    async def export(environment, context, store, export):
        artifacts = export["artifacts"]
        for item in artifacts.values():
            if len(store.read_blob(item["sha256"])) != item["size"]:
                raise Conflict("Harbor export size differs")
        Trajectory.model_validate_json(store.read_blob(artifacts["trajectory.json"]["sha256"]))
        await environment.upload_file(store.path(artifacts["submission.bundle"]["sha256"]),
                                      "/output/submission.bundle")
        usage = export["usage"] or {}
        context.n_input_tokens = usage.get("input_tokens")
        context.n_cache_tokens = usage.get("cached_input_tokens")
        context.n_output_tokens = usage.get("output_tokens")
        context.cost_usd = export["cost_usd"]
        context.metadata = {"factory_workflow": export["workflow_id"], "submission": export["submitted_sha"],
                            "atif_sha256": artifacts["trajectory.json"]["sha256"]}
