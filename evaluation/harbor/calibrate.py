import asyncio
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

from harbor.agents.base import BaseAgent
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig
from harbor.trial.trial import Trial

from factory.evidence import digest, encode
from evaluation.harbor.prepare import HERE, require, stage
from evaluation.harbor.receipt import CALIBRATION, validate_calibration


def docker(*args, input=None, timeout=30):
    return subprocess.check_output(["docker", *args], input=input, timeout=timeout).decode().strip()


class CalibrationAgent(BaseAgent):
    def __init__(self, *, bundle, base, tree, **kwargs):
        super().__init__(**kwargs)
        self.bundle = Path(bundle) if bundle else None
        self.base = base
        self.tree = tree
        self.proof = None

    @staticmethod
    def name():
        return "factory-calibration"

    def version(self):
        return "1"

    async def setup(self, environment):
        pass

    async def run(self, instruction, environment, context):
        probe = await environment.exec(command=f'''
set -eu
cd /work/product
test "$(id -u)" = 10001
test "$(git rev-parse HEAD)" = {shlex.quote(self.base)}
test "$(git rev-parse 'HEAD^{{tree}}')" = {shlex.quote(self.tree)}
test "$(git for-each-ref --format='%(refname)')" = refs/heads/work
test -z "$(git remote)"
test -z "$(git status --porcelain)"
unreachable=$(git fsck --full --unreachable --no-reflogs)
test -z "$unreachable"
test ! -e .git/objects/info/alternates
test ! -e src/factory/verified_patch.py
test ! -e grading/check_verified_patch.py
test ! -e /tests/grade.py
test ! -e /tests/expected.json
test ! -e /var/run/docker.sock
for protected in /seed /logs/verifier /logs/artifacts /usr/local; do
    if touch "$protected/p06-write-probe" 2>/dev/null; then
        printf 'unexpected writable path %s\n' "$protected" >&2
        exit 1
    fi
done
test -w /work/product
touch /output/probe
rm /output/probe
touch /tmp/agent-only
hostname
''')
        require(probe.return_code == 0, f"agent isolation or base probe failed: {probe}")
        container = probe.stdout.strip()
        require(re.fullmatch(r"[0-9a-f]{12}", container), "unexpected agent container hostname")
        inspect = json.loads(docker("inspect", container))[0]
        host = inspect["HostConfig"]
        require(host["ReadonlyRootfs"] and host["NetworkMode"] == "none" and not host["Privileged"]
                and host["PidsLimit"] == 64 and host["NanoCpus"] == 10**9 and host["Memory"] == 512 * 1024**2
                and "no-new-privileges:true" in host["SecurityOpt"],
                "agent container settings differ from the frozen task")
        mounts = {mount["Destination"]: mount["RW"] for mount in inspect["Mounts"] if mount["Type"] != "tmpfs"}
        require(mounts == {"/seed": False, "/output": True, "/logs/agent": True,
                           "/logs/artifacts": False, "/logs/verifier": False},
                f"unexpected agent mounts: {mounts}")
        self.proof = {
            "container": container, "seed_sha": self.base, "clean_base_objects_only": True,
            "network": "none", "root_read_only": True, "unprivileged_uid": 10001,
            "protected_paths_denied": True, "output_writable": True,
        }
        if self.bundle:
            await environment.upload_file(self.bundle, "/output/submission.bundle")
        context.n_input_tokens = context.n_output_tokens = context.n_cache_tokens = 0
        context.cost_usd = 0


async def calibrate(run_dir):
    pins, task, inputs = stage(run_dir, Path("/p03"), Path("/records"), Path("/held-out"))
    history = pins["historical"]
    require(version("harbor") == pins["harbor_version"], "Harbor version differs")
    require(docker("image", "inspect", history["workspace_image"], "--format", "{{.Id}}")
            == history["workspace_image"], "load the retained P03 workspace image by digest")
    alias = "factory-harbor-workspace:" + history["workspace_image"].removeprefix("sha256:")
    docker("tag", history["workspace_image"], alias)
    base = history["base_sha"]
    mutant = docker("run", "--rm", "--pull=never", "--network", "none", "--read-only",
                    "--tmpfs", "/tmp", "--cap-drop", "ALL", "-v", f"{inputs}:/calibration",
                    "-e", "PYTHONPATH=/factory/src", "-i", history["workspace_image"],
                    "python", "-", "/calibration", base, history["known_sha"],
                    input=(HERE / "mutate.py").read_bytes())
    assigned = {"known-solution": history["known_sha"], "unchanged-base": base,
                "regression": mutant, "missing-output": base}
    bundles = {"known-solution": inputs / "known-solution.bundle",
               "unchanged-base": task / "environment/base.bundle",
               "regression": inputs / "regression.bundle", "missing-output": None}

    grader_digest = digest(encode({
        "files": pins["grader"], "fixture_tree": history["records_tree"], "historical_grader": history["grader"],
        "expected_patch": history["expected_patch"], "workspace": history["workspace_image"], "base": base,
    }))
    task_digest = digest(encode(pins))
    empty = run_dir / "empty-agent-log-mount"
    empty.mkdir(mode=0o555)
    trials = []
    for name in CALIBRATION:
        output = run_dir / "outputs" / name
        output.mkdir(parents=True, mode=0o777)
        trial_name = f"p06-{run_dir.name.rsplit('.', 1)[-1].lower()}-{name}"
        trial = await Trial.create(TrialConfig(
            task=TaskConfig(path=task),
            trial_name=trial_name,
            trials_dir=run_dir / "trials",
            agent=AgentConfig(import_path="evaluation.harbor.calibrate:CalibrationAgent", kwargs={
                "bundle": str(bundles[name]) if bundles[name] else None, "base": base, "tree": history["base_tree"],
            }),
            environment=EnvironmentConfig(
                type="docker", cpu_enforcement_policy="limit", memory_enforcement_policy="limit",
                kwargs={"auto_mounting": True},
                mounts=[
                    {"type": "bind", "source": str(empty), "target": target, "read_only": True,
                     "bind": {"create_host_path": False}}
                    for target in ("/logs/artifacts", "/logs/verifier")
                ] + [{"type": "bind", "source": str(output), "target": "/output",
                      "bind": {"create_host_path": False}}],
            ),
        ))
        result = await trial.run()
        require(not result.exception_info, f"{name} Harbor trial failed; see {trial.paths.trial_dir}")
        require(result.verifier_environment_mode == "separate" and result.verifier_result,
                f"{name} did not run a separate Harbor verifier")
        proof = trial.agent.proof
        grade = json.loads((trial.paths.verifier_dir / "grade.json").read_bytes())
        verifier = grade.pop("isolation")
        require(proof and proof["container"] != verifier["container"] and all(verifier.values()),
                f"{name} did not prove isolation")
        expected_bundle = bundles[name]
        require(grade["bundle_sha256"] == (digest(expected_bundle.read_bytes()) if expected_bundle else None),
                f"{name} verifier saw different submission bytes")
        for scope in (proof, verifier):
            require(not docker("ps", "-aq", "--filter", f"id={scope['container']}"),
                    f"{name} left trial container {scope['container']} behind")
        reward = result.verifier_result.rewards["reward"]
        validate_calibration(name, grade, reward, assigned[name] if expected_bundle else None)
        require(result.agent_result and result.agent_result.n_input_tokens == result.agent_result.n_output_tokens == 0
                and result.agent_result.cost_usd == 0 and result.agent_info.model_info is None,
                f"{name} used model resources")
        receipt = {
            "case": name, "harbor_trial_id": str(result.id), "harbor_task_checksum": result.task_checksum,
            "task_sha256": task_digest, "grader_sha256": grader_digest, "assigned_sha": assigned[name],
            "reward": reward, **grade, "agent_isolation": proof, "verifier_isolation": verifier,
        }
        (run_dir / f"{name}.json").write_bytes(encode(receipt))
        trials.append(receipt)
        print(f"{name} {grade['status']} {grade['reason']} {result.id}", flush=True)
    return {
        "schema_version": 1, "tested_sha": os.environ["HARBOR_FACTORY_SHA"],
        "clean_checkout": os.environ["HARBOR_FACTORY_CLEAN"] == "true",
        "task_sha256": task_digest, "grader_sha256": grader_digest,
        "harbor_revision": pins["harbor_revision"], "workspace_image": history["workspace_image"],
        "docker_server_version": docker("version", "--format", "{{.Server.Version}}"),
        "compose_version": docker("compose", "version", "--short"),
        "resources": {"trials": len(trials), "agent_containers": len(trials), "verifier_containers": len(trials),
                      "controller_containers": 1, "preparation_containers": 1, "model_calls": 0, "retries": 0,
                      "remaining_trial_containers": 0},
        "trials": trials,
    }


if __name__ == "__main__":
    run_dir = Path(os.environ["HARBOR_RUN_DIR"])
    summary = asyncio.run(calibrate(run_dir))
    (run_dir / "summary.json").write_bytes(encode(summary))
