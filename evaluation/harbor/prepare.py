from pathlib import Path
import json
import shutil

from factory.checkpoint import decode
from factory.evidence import Evidence, digest, encode


ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "evaluation/harbor"
TASK = HERE / "tasks/verified-patch"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def checked(path, expected):
    require(not path.is_symlink(), f"unexpected symlink at {path.name}")
    data = path.read_bytes()
    require(digest(data) == expected, f"digest mismatch for {path.name}")
    return data


def record_tree(root):
    files = {}
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "symlink in protected fixture")
        if path.is_file() and path.name != ".lock":
            files[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    return files


def stage(run_dir, p03, records, held_out):
    pins = json.loads((HERE / "frozen.json").read_bytes())
    history = pins["historical"]
    for section in ("task", "grader", "runtime"):
        for path, expected in pins[section].items():
            checked(ROOT / path, expected)
    split = json.loads((HERE / "split.json").read_bytes())
    for case in split["held_out"]:
        for filename, expected in case["inputs"].items():
            checked(held_out / case["id"] / filename, expected)

    p03_run = p03 / "runs" / history["run_id"]
    request = json.loads(checked(p03_run / "request.json", history["request"]))
    recovery = json.loads(checked(p03_run / "verification-recovery.json", history["recovery"]))
    accepted = json.loads((p03_run / "maintainer-acceptance.json").read_bytes())
    require(accepted["decision"] == "accepted" and accepted["submitted_sha"] == history["known_sha"]
            and accepted["verification_manifest_digest"] == history["recovery"],
            "P03 maintainer acceptance did not match")
    require(request["base_sha"] == history["base_sha"] and request["workspace_image"] == history["workspace_image"]
            and recovery["request_digest"] == history["request"]
            and recovery["submitted_sha"] == history["known_sha"]
            and recovery["status"] == "verified" and not recovery["missing_artifacts"]
            and recovery["artifacts"]["grader"] == request["grader_digest"] == history["grader"],
            "P03 request and verification disagree")
    require((TASK / "instruction.md").read_text().startswith(request["outcome"]),
            "Harbor task must preserve the approved outcome")
    store = Evidence(p03)
    require(store.read_blob(history["grader"]) == (ROOT / "grading/check_verified_patch.py").read_bytes(),
            "P03 grader source differs")
    verdict = json.loads(store.read_blob(recovery["artifacts"]["independent_verdict"]))
    require(verdict["decision"] == "accepted" and verdict["checked_sha"] == history["known_sha"]
            and verdict["expected_patch_sha256"] == history["expected_patch"]
            and verdict["grader_digest"] == history["grader"] and all(row["passed"] for row in verdict["checks"]),
            "P03 verdict did not pass the frozen checks")
    profile = json.loads(store.read_blob(request["configuration"]))
    require(profile["skills"] == history["skills"] and profile["factory_sha"] == history["factory_sha"],
            "P03 skill provenance differs")

    base = json.loads(store.read_blob(request["base_checkpoint"]))
    require(base["head_sha"] == base["base_sha"] == history["base_sha"] and base["tree"] == history["base_tree"]
            and not any(base[key] for key in ("index_patch", "working_patch", "untracked")),
            "P03 base checkpoint differs or is dirty")
    fixture_files = record_tree(records)
    require(digest(encode(fixture_files)) == history["records_tree"], "records fixture differs")
    fixture = (records / "runs" / request["fixture_run"] / "manifest.json").read_bytes()
    fixture_bundle = json.loads(fixture)["artifacts"]["submitted_bundle"]

    task_dir = run_dir / TASK.name
    for section in ("task", "grader"):
        for name, expected in pins[section].items():
            source = ROOT / name
            if source.is_relative_to(TASK):
                destination = task_dir / source.relative_to(TASK)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(checked(source, expected))
    agent = task_dir / "environment"
    (agent / "base.bundle").write_bytes(decode(base["bundle"]))
    protected = task_dir / "tests" / "protected"
    protected.mkdir(mode=0o700)
    shutil.move(task_dir / "tests" / "test.sh", protected)
    shutil.move(task_dir / "tests" / "grade.py", protected)
    (protected / "test.sh").chmod(0o755)
    verifier = "src/factory/verify.py"
    (protected / "verify.py").write_bytes(checked(ROOT / verifier, pins["grader"][verifier]))
    (protected / "expected.json").write_bytes(encode({
        "base_sha": history["base_sha"], "expected_patch_sha256": history["expected_patch"],
        "records_bundle_sha256": fixture_bundle, "records_run": request["fixture_run"],
    }))
    for relative, expected in fixture_files.items():
        path = protected / "archive" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(checked(records / relative, expected))

    inputs = run_dir / "calibration"
    inputs.mkdir(mode=0o700)
    (inputs / "known-solution.bundle").write_bytes(store.read_blob(recovery["artifacts"]["submitted_bundle"]))
    return pins, task_dir, inputs
