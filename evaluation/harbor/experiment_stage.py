"""Build and calibrate the frozen held-out tasks before comparison dispatch."""
import asyncio
from importlib.metadata import version
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

from harbor.models.task.task import Task
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig
from harbor.trial.trial import Trial

from evaluation.harbor.bridge_stage import main as stage_historical
from evaluation.harbor.calibrate import CalibrationAgent, docker
from evaluation.harbor.prepare import HERE, TASK, checked, record_tree, require
from factory.evidence import Evidence, digest, encode
from factory.harbor_receipt import ISOLATION_CHECKS


class HoldoutCalibrationAgent(CalibrationAgent):
    async def run(self, instruction, environment, context):
        probe = await environment.exec(
            command=f"test -z \"$(git status --porcelain)\" && test -z \"$(git remote)\" && "
                    f"test \"$(git rev-parse HEAD)\" = {self.base} && test ! -e /tests && hostname",
            cwd="/work/product", user="10001:10001", timeout_sec=10,
        )
        require(probe.return_code == 0, "held-out calibration workspace differs")
        self.proof = {"container": probe.stdout.strip()}
        await environment.upload_file(self.bundle, "/output/submission.bundle")
        context.n_input_tokens = context.n_output_tokens = context.n_cache_tokens = 0
        context.cost_usd = 0


def git_in_image(image, controls, *command):
    return subprocess.check_output([
        "docker", "run", "--rm", "--pull=never", "--network", "none", "--read-only",
        "--tmpfs", "/tmp:size=96m", "--cap-drop", "ALL", "-v", f"{controls}:/controls:ro",
        "-e", "HOME=/tmp", image, *command,
    ], timeout=30)


def stage_case(store, split, historical):
    name = split["id"]
    base = split["base_sha"]
    controls = store.root / "controls" / name
    pin = json.loads((controls / "pin.json").read_bytes())
    require(pin["base"]["sha"] == base, "held-out control must use the frozen base")
    for control in ("base", "known", "regression"):
        data = checked(controls / f"{control}.bundle", pin[control]["bundle"])
        refs = git_in_image(historical["workspace_image"], controls,
                            "git", "bundle", "list-heads", f"/controls/{control}.bundle")
        require(refs.decode().split() == [pin[control]["sha"], "refs/heads/submission"],
                "held-out control bundle differs")
        store.blob(data)
    task = store.root / name
    shutil.copytree(Path(historical["task"]) / "environment", task / "environment")
    bundle = task / "environment/base.bundle"
    bundle.write_bytes((controls / "base.bundle").read_bytes())
    config = (TASK / "task.toml").read_text().replace("verified-patch-v1", name + "-v1").replace(
        "Retrieve the verified Git patch for an archived records delivery.", "Complete one operator request."
    ).replace('split = "tuning"', 'split = "held_out"').replace('difficulty = "medium"', f'difficulty = "{split["difficulty"]}"')
    (task / "task.toml").write_text(config)
    instruction = checked(Path("/held-out") / name / "instruction.md", split["inputs"]["instruction.md"])
    (task / "instruction.md").write_bytes(instruction)
    protected = task / "tests/protected"
    protected.mkdir(parents=True)
    shutil.copyfile(TASK / "tests/docker-compose.yaml", task / "tests/docker-compose.yaml")
    (protected / "checks.py").write_bytes(checked(Path("/held-out") / name / "checks.py", split["inputs"]["checks.py"]))
    shutil.copyfile(Path(historical["task"]) / "tests/protected/verify.py", protected / "verify.py")
    shutil.copyfile(TASK / "tests/grade.py", protected / "historical_grade.py")
    shutil.copyfile(HERE / "holdout_grade.py", protected / "grade.py")
    shutil.copyfile(TASK / "tests/test.sh", protected / "test.sh")
    (protected / "test.sh").chmod(0o755)
    reference = git_in_image(historical["workspace_image"], controls, "sh", "-ec",
                             'git init -q /tmp/reference; cd /tmp/reference; '
                             'git fetch -q /controls/base.bundle refs/heads/submission; '
                             'test "$(git rev-parse FETCH_HEAD)" = "$1"; git archive FETCH_HEAD src',
                             "reference", base)
    with tarfile.open(fileobj=BytesIO(reference)) as files:
        files.extractall(protected / "reference", filter="data")
    grader = store.blob(encode({"base": base, "workspace": historical["workspace_image"],
                               "compose": digest((task / "tests/docker-compose.yaml").read_bytes()),
                               "checks": record_tree(protected)}))
    (protected / "expected.json").write_bytes(encode({"base_sha": base, "grader_digest": grader}))
    return {
        "task": str(task), "task_checksum": Task(task).checksum, "task_digest": store.blob(encode(record_tree(task))),
        "grader_digest": grader, "reported_grader_required": True, "grade_checks": ["contract"],
        "base_sha": base, "workspace_image": historical["workspace_image"], "instruction": instruction.decode(),
        "harbor_revision": historical["harbor_revision"],
    }


async def calibrate(store, lock, name):
    results = []
    controls = store.root / "controls" / name
    pin = json.loads((controls / "pin.json").read_bytes())
    folder = store.root / "calibration" / name
    empty = folder / "empty"
    empty.mkdir(parents=True)
    for case, accepted in (("known", True), ("base", False), ("regression", False)):
        bundle = controls / f"{case}.bundle"
        output = folder / case
        output.mkdir(mode=0o777)
        trial = await Trial.create(TrialConfig(
            task=TaskConfig(path=lock["task"]), trials_dir=folder,
            agent=AgentConfig(import_path="evaluation.harbor.experiment_stage:HoldoutCalibrationAgent",
                              kwargs={"bundle": str(bundle), "base": lock["base_sha"], "tree": None}),
            environment=EnvironmentConfig(
                type="docker", cpu_enforcement_policy="limit", memory_enforcement_policy="limit",
                kwargs={"auto_mounting": True},
                mounts=[
                    {"type": "bind", "source": str(empty), "target": target, "read_only": True,
                     "bind": {"create_host_path": False}} for target in ("/logs/artifacts", "/logs/verifier")
                ] + [{"type": "bind", "source": str(output), "target": "/output",
                      "bind": {"create_host_path": False}}],
            ),
        ))
        result = await trial.run()
        require(not result.exception_info and result.verifier_environment_mode == "separate",
                f"{name} {case} calibration failed: {trial.paths.trial_dir}")
        grade_data = (trial.paths.verifier_dir / "grade.json").read_bytes()
        grade = json.loads(grade_data)
        reward = result.verifier_result.rewards["reward"]
        require(result.task_checksum == lock["task_checksum"] and reward == int(accepted)
                and grade["checks"] == {"contract": accepted}
                and grade["status"] == ("accepted" if accepted else "rejected")
                and grade["submitted_sha"] == pin[case]["sha"]
                and grade["bundle_sha256"] == pin[case]["bundle"]
                and grade["grader_digest"] == lock["grader_digest"]
                and all(grade["isolation"].get(key) is True for key in ISOLATION_CHECKS)
                and grade["isolation"]["container"] != trial.agent.proof["container"]
                and result.agent_result.cost_usd == result.agent_result.n_input_tokens == result.agent_result.n_output_tokens == 0
                and not docker("ps", "-aq", "--filter", f"id={trial.agent.proof['container']}")
                and not docker("ps", "-aq", "--filter", f"id={grade['isolation']['container']}"),
                f"{name} {case} calibration disagrees with the frozen contract")
        results.append({"case": case, "accepted": accepted, "grade": store.blob(grade_data),
                        "sha": grade["submitted_sha"], "harbor_trial_id": str(result.id)})
        print(name, case, grade["status"], result.id, flush=True)
    return results


async def main():
    root = Path(os.environ["BRIDGE_ROOT"])
    store = Evidence(root)
    stage_historical()
    frozen = store.read("bridge", "frozen", required=True)
    pins = json.loads((HERE / "frozen.json").read_bytes())
    require(version("harbor") == pins["harbor_version"], "Harbor version drift")
    historical = {**frozen, "task_tree": digest(encode(record_tree(Path(frozen["task"]))))}
    tasks = {"verified-patch": historical}
    for case in json.loads((HERE / "split.json").read_bytes())["held_out"]:
        lock = stage_case(store, case, historical)
        results = await calibrate(store, lock, case["id"])
        tasks[case["id"]] = {**lock, "task_tree": lock["task_digest"], "calibration": results}
    store.record("experiment", "tasks", tasks)


if __name__ == "__main__":
    asyncio.run(main())
