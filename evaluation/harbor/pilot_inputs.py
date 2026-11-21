"""Copy pinned Phase 09 inputs into an isolated, trusted evidence store."""
import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from evaluation.harbor.experiment_inputs import prepare
from evaluation.harbor.prepare import HERE, checked, record_tree, require
from factory.agent_profile import resolve
from factory.checkpoint import encode as encode_bundle, working_state
from factory.evidence import Conflict, Evidence, digest, encode, validate_run_id
from factory.fresh_pilot import require_approval, task_instruction
from factory.verify import Rejected, reconstruct


RUN = "experiment"


def frozen_design(store, path, expected):
    raw = checked(path, expected)
    design = json.loads(raw)
    store.record(RUN, "pilot-design", {"sha256": store.blob(raw)})
    return design


def protected_path(path, host_root, mount):
    relative = Path(path).relative_to(host_root)
    local = mount / relative
    require(local.resolve() == local and local.is_relative_to(mount),
            "protected input escaped its read-only mount")
    return local


def checkpoint_from_bundle(bundle, base):
    with TemporaryDirectory() as folder:
        checkout = Path(folder)
        try:
            reconstruct(checkout, bundle, base, base)
        except Rejected as error:
            raise Conflict(f"staged base bundle does not match its lock: {error}") from error
        state = working_state(checkout)
        return {"base_sha": base, "head_sha": base, "bundle": encode_bundle(bundle.read_bytes()), **state}


def stage_task(store, case, lock, host_root, mount):
    from harbor.models.task.task import Task

    name = validate_run_id(case["id"])
    source = protected_path(lock["task"], host_root, mount)
    destination = store.root / name
    require(digest(encode(record_tree(source))) == lock["task_tree"],
            f"{name} protected task digest changed")
    shutil.copytree(source, destination)
    tree = encode(record_tree(destination))
    require(store.blob(tree) == lock["task_tree"] == lock["task_digest"]
            and Task(destination).checksum == lock["task_checksum"],
            f"{name} task tree or checksum changed while staging")
    base = lock["base_bundle"]
    base_path = protected_path(base["path"], host_root, mount)
    require(base_path == source / "environment/base.bundle", f"{name} staged base is not the locked base")
    staged_base = destination / "environment/base.bundle"
    store.blob(checked(staged_base, base["sha256"]))
    instruction = (destination / "instruction.md").read_text()
    require(instruction == lock["instruction"] == task_instruction(case["prompt"])
            and digest(instruction.encode()) == lock["prompt_sha256"],
            f"{name} prompt differs from the approved design")
    descriptor = protected_path(lock["grader_descriptor"], host_root, mount)
    store.blob(checked(descriptor, lock["grader_digest"]))
    staged = {}
    for control, pin in lock["staged"].items():
        bundle = protected_path(pin["bundle"], host_root, mount)
        store.blob(checked(bundle, pin["bundle_sha256"]))
        staged[control] = {key: value for key, value in pin.items() if key != "bundle"}
    require(staged["base"]["sha"] == lock["base_sha"] and staged["base"]["bundle_sha256"] == base["sha256"]
            and staged["base"]["tree"] == lock["upstream"]["base_tree"]
            and staged["known"]["tree"] == case["known_tree"],
            f"{name} staged Git identities differ from the approved upstream trees")
    calibration = []
    for row in lock["calibration"]:
        artifacts = {"path": "grade", "receipt": "receipt_sha256", "contract_log": "contract_log_sha256"}
        for path, reference in artifacts.items():
            artifact = protected_path(row[path], host_root, mount)
            store.blob(checked(artifact, row[reference]))
        grade = json.loads(store.read_blob(row["grade"]))
        control = staged[row["case"]]
        require(grade["bundle_sha256"] == control["bundle_sha256"] and row["sha"] == control["sha"],
                f"{name} calibration did not grade the locked control bundle")
        calibration.append({key: value for key, value in row.items() if key not in artifacts})
    output = {key: value for key, value in lock.items() if key != "grader_descriptor"}
    return {**output, "task": str(destination), "base_bundle": {**base, "path": str(staged_base)},
            "staged": staged, "calibration": calibration}


def stage(store, design, protected):
    from evaluation.harbor.bridge_stage import main as stage_historical

    pins = json.loads((HERE / "frozen.json").read_bytes())
    require(version("harbor") == pins["harbor_version"], "Harbor version drift")
    stage_historical()
    frozen = store.read("bridge", "frozen", required=True)
    tree = store.blob(encode(record_tree(Path(frozen["task"]))))
    historical = {**frozen, "task_tree": tree}
    tasks = {"verified-patch": historical}
    if protected:
        mount = Path("/pilot-protected")
        locks = json.loads((mount / "lock.json").read_bytes())
        cases = design["tasks"]["held_out"]
        require(set(locks) == {case["id"] for case in cases}, "protected task locks differ from the design")
        for case in cases:
            name = case["id"]
            tasks[name] = stage_task(store, case, locks[name], protected, mount)
    store.record(RUN, "tasks", tasks)


def configure(store, build, worker, mode, cli_build, source):
    design_ref = store.read(RUN, "pilot-design", required=True)["sha256"]
    design = json.loads(store.read_blob(design_ref))
    tasks = store.read(RUN, "tasks", required=True)
    bases = {}
    for name, task in tasks.items():
        base = checkpoint_from_bundle(Path(task["task"]) / "environment/base.bundle", task["base_sha"])
        if "upstream" in task and task["upstream"]["base_tree"] != base["tree"]:
            raise Conflict(f"{name} base checkpoint differs from the locked upstream tree")
        bases[name] = base
    archive = Path("/p03")
    baseline = design["treatment"]["variants"]["baseline"]["factory_sha"]
    transport = None
    if mode == "live":
        active = resolve(source, build, store)
        if active != {**resolve(source, baseline, store), "factory_sha": build}:
            raise Conflict("the frozen baseline differs from the active effective configuration")
        transport = store.read("bridge", "frozen", required=True)["historical_transport"]
        store.blob(Evidence(archive).read_blob(transport["ca_digest"]))
    prepare(store.root, archive, baseline, build, worker, mode, source_repo=source, cli_build=cli_build,
            trusted_transport=transport, checkpoints=bases)


def approve(store, approval_path):
    manifest = store.read(RUN, "frozen", required=True)
    approval = json.loads(approval_path.read_bytes())
    if not isinstance(approval, dict) or approval.keys() != {"approved_by", "digest", "envelope", "design_sha256"}:
        raise Conflict("invalid pilot approval record")
    require_approval(manifest, approval)
    store.record(RUN, "approval", approval)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    actions = parser.add_subparsers(dest="action", required=True)
    staging = actions.add_parser("stage")
    staging.add_argument("design", type=Path)
    staging.add_argument("digest")
    staging.add_argument("--protected", type=Path)
    configuration = actions.add_parser("configure")
    configuration.add_argument("build")
    configuration.add_argument("worker")
    configuration.add_argument("mode", choices=("scripted", "live"))
    configuration.add_argument("cli_build")
    configuration.add_argument("source", type=Path)
    approval = actions.add_parser("approve")
    approval.add_argument("file", type=Path)
    args = parser.parse_args()
    store = Evidence(Path(os.environ["BRIDGE_ROOT"]))
    if args.action == "stage":
        stage(store, frozen_design(store, args.design, args.digest), args.protected)
    elif args.action == "configure":
        configure(store, args.build, args.worker, args.mode, args.cli_build, args.source)
    else:
        approve(store, args.file)


if __name__ == "__main__":
    main()
