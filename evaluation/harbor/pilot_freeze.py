import json
import re

from factory.evidence import Conflict
from factory.fresh_pilot import PILOT_ENVELOPE, assignment_rows, task_instruction
from factory.harbor_experiment import ARMS, FRESH_PILOT_THRESHOLDS, TUNING
from factory.harbor_receipt import ISOLATION_CHECKS


RUN = "experiment"
CONTROL_VERDICTS = {"known": True, "base": False, "regression": False}


def verify_calibration(store, task):
    rows = task["calibration"]
    cases = {row["case"]: row for row in rows}
    if len(rows) != len(CONTROL_VERDICTS) or cases.keys() != CONTROL_VERDICTS.keys():
        raise Conflict("fresh pilot requires the three offline control grades for every held-out task")
    if cases["base"]["sha"] != task["base_sha"] or len({row["sha"] for row in rows}) != len(rows):
        raise Conflict("offline control revisions differ from the staged task")
    for case, accepted in CONTROL_VERDICTS.items():
        row = cases[case]
        expectation = task["calibration_expectations"][case]
        grade = json.loads(store.read_blob(row["grade"]))
        checks = grade["checks"]
        if (row["accepted"] is not accepted or expectation["accepted"] is not accepted
                or (not accepted and not expectation["required_fail"])
                or grade["submitted_sha"] != row["sha"] or row["sha"] != task["staged"][case]["sha"]
                or grade["status"] != ("accepted" if accepted else "rejected")
                or grade["grader_digest"] != task["grader_digest"]
                or set(checks) != set(task["grade_checks"])
                or any(type(value) is not bool for value in checks.values()) or all(checks.values()) != accepted
                or any(checks.get(name) is not True for name in expectation["required_pass"])
                or any(checks.get(name) is not False for name in expectation["required_fail"])
                or any(grade["isolation"].get(name) is not True for name in ISOLATION_CHECKS)):
            raise Conflict("offline control grade differs from the frozen acceptance contract")


def verify_configuration(store, design, configuration, mode):
    if set(configuration["arms"]) != set(ARMS) or configuration["tools"] != design["runtime"]["tools"]:
        raise Conflict("fresh pilot bot selector or configured tools changed")
    if any(configuration["source"][key] != design["source"][f"phase05_{key}"]
           for key in ("candidate_diff", "candidate_manifest")):
        raise Conflict("fresh pilot candidate differs from Phase 05")
    transport = configuration["transport"]
    if mode == "scripted":
        if transport != {"kind": "scripted-responses"}:
            raise Conflict("offline pilot requires the scripted transport")
    elif any(transport.get(key) != value for key, value in design["runtime"]["transport"].items()):
        raise Conflict("fresh pilot subscription route differs from the design")
    tested_sha = (store.root / "tested-sha.txt").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", tested_sha):
        raise Conflict("fresh pilot requires a pinned worker source revision")
    worker_images = set()
    for name, arm in configuration["arms"].items():
        variant = design["treatment"]["variants"][name]
        runtime = arm["runtime"]
        if (arm["factory_sha"] != variant["factory_sha"] or arm["configuration"] != variant["configuration_sha256"]
                or runtime["profile_digest"] != variant["selector_sha256"]
                or runtime["skills"] != variant["skill_files"] or runtime["transport"] != transport
                or runtime["workflow_build"] != tested_sha or runtime["api_fallback"] is not False
                or any(runtime[key] != design["runtime"][key]
                       for key in ("workflow", "cli_build", "model", "reasoning_effort"))):
            raise Conflict("fresh pilot variant or runtime differs from the approved design")
        worker_images.add(runtime["worker_image"])
    if len(worker_images) != 1 or not re.fullmatch(r"sha256:[0-9a-f]{64}", next(iter(worker_images))):
        raise Conflict("fresh pilot arms require the same pinned worker image")


def pilot_manifest(store, mode):
    if mode not in ("scripted", "live"):
        raise Conflict("invalid fresh pilot mode")
    design_digest = store.read(RUN, "pilot-design", required=True)["sha256"]
    design = json.loads(store.read_blob(design_digest))
    assignments = assignment_rows(design)
    configuration = store.read(RUN, "configuration", required=True)
    verify_configuration(store, design, configuration, mode)
    tasks = store.read(RUN, "tasks", required=True)
    held_out = {task["id"]: task for task in design["tasks"]["held_out"]}
    if (TUNING not in tasks or not tasks.keys() <= (held_out.keys() | {TUNING})
            or (mode == "live" and tasks.keys() != (held_out.keys() | {TUNING}))
            or configuration["base_checkpoints"].keys() != tasks.keys()):
        raise Conflict("fresh pilot is missing required staged task inputs")
    historical_pins = design["tasks"]["historical"]["pins"]
    if any(tasks[TUNING].get(key) != value for key, value in historical_pins.items() if key != "calibration"):
        raise Conflict("fresh pilot historical control differs from the approved design")
    for name in held_out.keys() & tasks.keys():
        task, pins = tasks[name], held_out[name]
        if (task["instruction"] != task_instruction(pins["prompt"]) or task["reported_grader_required"] is not True
                or any(task["upstream"][key] != pins[key]
                       for key in ("repository", "base_sha", "known_sha", "known_tree"))):
            raise Conflict("fresh pilot staged task differs from the approved upstream contract")
        verify_calibration(store, task)
    harbor_image = (store.root / "harbor-image.txt").read_text().strip()
    if harbor_image != design["runtime"]["harbor_image"]:
        raise Conflict("fresh pilot Harbor image differs from the design")
    return {
        "schema_version": 2, "design_sha256": design_digest, "mode": mode,
        "envelope": PILOT_ENVELOPE if mode == "live" else {
            **PILOT_ENVELOPE, "max_executions": 2, "execution_seconds": 120, "batch_seconds": 900,
            "subscription_executions": 0,
        },
        "assignments": assignments, "tasks": tasks, "configuration": configuration, "harbor_image": harbor_image,
        "thresholds": FRESH_PILOT_THRESHOLDS,
        "scripted_dispatch": [row["id"] for row in assignments[:2]] if mode == "scripted" else [],
    }
