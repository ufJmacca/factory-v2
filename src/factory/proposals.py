"""Propose the P05 pilot correction from completed delivery evidence."""

import argparse
from dataclasses import asdict, dataclass, replace
from difflib import unified_diff
import json
from pathlib import Path
import re
import subprocess
from tarfile import TarError, open as open_tar
from tempfile import TemporaryDirectory

from factory.checkpoint import restore
from factory.codex_bridge import Bridge
from factory.codex_delivery import prompt
from factory.codex_evidence import reopen
from factory.delivery_steps import require
from factory.evidence import Conflict, digest, encode, publish
from factory.product import reopen_product
from factory.proposal_evidence import (
    Observation, READ_COMMAND, REJECTED, ReadOnlyArchive, WorkspaceCall, diagnose, workspace_retry,
)


RUN = "pr03-proxy-final"
SKILL = ".agents/skills/delivery-evidence/SKILL.md"
BOT = "delivery-engineer"
REJECTED_LINE, RETRY_LINE = 19, 21
PRODUCER = "propose-correction"
ADDITION = (
    "\nSend the workspace tool exactly one argument, `command`. Put the entire\n"
    "shell command in that string, including multiline scripts. Omit every other\n"
    "field. If the tool rejects the arguments, check the field names before retrying.\n"
)


@dataclass(frozen=True)
class PilotEvidence:
    source: dict
    task: dict
    profile: dict
    checkpoint: dict
    calls: tuple[WorkspaceCall, WorkspaceCall]
    pointers: tuple[dict, dict]
    feedback_pointer: dict
    recorded_at: str


def load_pilot(product: ReadOnlyArchive, codex: ReadOnlyArchive) -> PilotEvidence:
    observation = reopen_product(product, RUN)
    manifest = reopen(codex, RUN, "verification-recovery")
    task = codex.read(RUN, "request", required=True)
    source = observation["source"]
    require(observation["status"] == "observed" and manifest["status"] == "verified"
            and observation["accepted_revision"] == manifest["submitted_sha"]
            and source["verification_recovery"] == digest(encode(manifest)),
            "product observation and Codex submission do not match")
    profile = json.loads(codex.read_blob(source["configuration"]))
    require(profile["bot"] == BOT and profile["factory_sha"] == task["factory_sha"]
            and source["configuration"] == task["configuration"] and SKILL in profile["skills"],
            "the pilot requires the pinned delivery-evidence skill")

    reference = source["attempt-1_events"]
    require(reference == manifest["artifacts"]["attempt-1_events"], "native trace does not match the product")
    events = codex.read_blob(reference)
    calls = workspace_retry(events, REJECTED_LINE, RETRY_LINE)
    require(calls is not None, "no verified rejected workspace call and successful retry")
    pointers = tuple({"run_id": RUN, "artifact": "attempt-1.jsonl", "sha256": reference,
                      "line": call.line, "item_id": call.item_id} for call in calls)

    feedback = observation["feedback"]
    require(isinstance(feedback, list) and feedback and feedback[0]["revision"] == manifest["submitted_sha"],
            "missing revision-bound operator feedback")
    feedback_pointer = {"run_id": RUN, "record": "product-manifest.json",
                        "sha256": digest(encode(observation)), "pointer": "/feedback/0"}
    checkpoint = json.loads(codex.read_blob(source["base_checkpoint"]))
    require(checkpoint["head_sha"] == checkpoint["base_sha"] == task["base_sha"],
            "reproduction checkpoint differs from the approved task base")
    require(codex.read_blob(profile["skills"][SKILL]) in prompt(codex, task, profile),
            "the original prompt does not contain the resolved skill")
    return PilotEvidence(source, task, profile, checkpoint, calls, pointers, feedback_pointer,
                         manifest["completed_at"])


class ReplayWorkspace:
    def __init__(self, root: Path):
        self.root = root
        self.calls = 0

    def run(self, argv: list[str]) -> dict:
        require(argv == ["/bin/sh", "-c", READ_COMMAND], "replay allows only the frozen read command")
        self.calls += 1
        result = subprocess.run(["cat", "tests/test_recovery.py"], cwd=self.root, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=10, check=False)
        return {"exit_code": result.returncode, "output": "", "truncated": False}


def reproduce(evidence: PilotEvidence) -> dict:
    with TemporaryDirectory() as directory:
        root = Path(directory) / "product"
        restore(root, evidence.checkpoint)
        workspace = ReplayWorkspace(root)
        bridge = Bridge(ReadOnlyArchive(Path(directory)), RUN, workspace, evidence.task["base_sha"])
        try:
            bridge.call("workspace", evidence.calls[0].arguments)
        except Conflict as error:
            require(str(error) == REJECTED and workspace.calls == 0, "different workspace rejection")
        else:
            raise Conflict("the workspace no longer rejects the observed arguments")
        retry = bridge.call("workspace", evidence.calls[1].arguments)
        require(retry["exit_code"] == 0 and workspace.calls == 1, "the comparison replay did not succeed")
    return {"rejected_before_execution": True, "retry_exit_code": 0, "workspace_executions": 1}


def candidate_patch(before: bytes) -> tuple[str, bytes]:
    require(before.endswith(b"\n") and ADDITION.encode() not in before, "candidate skill cannot be extended")
    after = before + ADDITION.encode()
    diff = "".join(unified_diff(before.decode().splitlines(keepends=True), after.decode().splitlines(keepends=True),
                                fromfile=f"a/{SKILL}", tofile=f"b/{SKILL}"))
    return f"diff --git a/{SKILL} b/{SKILL}\n{diff}", after


def baseline_inputs(archive: Path, revision: str) -> tuple[bytes, bytes]:
    require(re.fullmatch(r"[0-9a-f]{40}", revision) is not None, "baseline must be a full Git commit SHA")
    with open_tar(archive, mode="r:") as tar:
        require(tar.pax_headers.get("comment") == revision, "baseline archive does not identify the pinned commit")
        entries = []
        for path in (SKILL, "agents.toml"):
            entry = tar.getmember(path)
            require(entry.isfile(), "baseline input must be a regular file")
            with tar.extractfile(entry) as stream:
                entries.append(stream.read())
    return entries[0], entries[1]


def propose(evidence: PilotEvidence, revision: str, skill: bytes, selector: bytes, reproduction: dict) -> dict:
    require(digest(skill) == evidence.profile["skills"][SKILL],
            "the implicated skill has changed since the observed run")
    patch, candidate_skill = candidate_patch(skill)
    failed = Observation(required=True, present=True, resolved=True, tool_failed=True,
                         outcome_defined=True, success=False)
    control = replace(failed, tool_failed=False, success=True)
    finding = {"event": evidence.pointers[0], "diagnosis": diagnose(failed)}
    finding_id = "p05-" + digest(encode(finding))
    common = {"schema_version": 1, "producer": PRODUCER, "recorded_at": evidence.recorded_at}
    baseline = {"factory_commit": revision, "bot": BOT, "selector_sha256": digest(selector),
                "skill_sha256": digest(skill)}
    manifest = {**common, "baseline": baseline, "path": SKILL, "sha256": digest(candidate_skill),
                "diff_sha256": digest(patch.encode())}
    prediction = {
        "metric": "count of initial workspace calls rejected for extra argument keys",
        "expected": "At least one fewer rejection in the candidate arm across three completed pairs.",
        "disproves": "The candidate records as many or more rejections than the baseline across the three pairs.",
        "required_regression_check": "Every candidate submission passes the frozen grader on its exact revision.",
    }
    proposal = {
        **common, "id": finding_id, "source_run_ids": [RUN], "event_pointers": list(evidence.pointers),
        "operator_feedback": evidence.feedback_pointer, "source_factory_commit": evidence.task["factory_sha"],
        "diagnosis": finding["diagnosis"], "status": "reproduced",
        "cause": "The workspace bridge rejected an extra argument before running the shell command.",
        "competing_explanation": "Request generation or transport may have added the field. The trace cannot distinguish them.",
        "skill_cause": "unknown",
        "skill_provenance": {
            "run_id": RUN, "configuration_sha256": evidence.source["configuration"],
            "skill_sha256": evidence.profile["skills"][SKILL], "present_in_reconstructed_prompt": True,
            "model_context": "unknown",
        },
        "observations": [asdict(failed), asdict(control)],
        "comparison_diagnosis": diagnose(control), "reproduction": reproduction, "affected_skill": SKILL,
        "baseline": baseline, "candidate_manifest_sha256": digest(encode(manifest)),
        "experiment_request_id": f"{finding_id}-experiment",
        "prediction": prediction, "risk": "The tool already declares the schema. Repeating it in the skill may have no effect.",
        "review_state": "pending", "activation_decision": "separate",
    }
    experiment = {
        **common, "id": proposal["experiment_request_id"], "proposal_id": finding_id,
        "proposal_sha256": digest(encode(proposal)), "baseline": baseline,
        "candidate_manifest_sha256": proposal["candidate_manifest_sha256"],
        "changed_input": {"path": SKILL, "baseline_sha256": digest(skill), "candidate_sha256": digest(candidate_skill)},
        "fixed_inputs": {
            "task_role": "historical_tuning", "task_run_id": RUN,
            "task_base": evidence.task["base_sha"], "task_base_checkpoint": evidence.source["base_checkpoint"],
            "task_contract_sha256": digest(encode(evidence.task)),
            "outcome_sha256": digest(evidence.task["outcome"].encode()), "grader_sha256": evidence.source["grader"],
            "cli_build": evidence.profile["cli_build"], "model": evidence.profile["model"],
            "reasoning_effort": evidence.profile["reasoning_effort"],
            "auth_mode": evidence.profile["auth_mode"],
            "transport_kind": evidence.task["transport"]["kind"],
            "transport_sha256": digest(encode(evidence.task["transport"])),
            "workspace_image": evidence.task["workspace_image"],
            "worker_image": evidence.task["worker_image"], "tools": evidence.profile["tools"],
        },
        "pairs": 3, "max_regression_count": 0, "limits": {"max_attempts_per_arm": 1, "max_minutes_per_arm": evidence.task["cap_minutes"],
                               "max_concurrent_runs": 1},
        "prediction": prediction, "incomplete_result": "Inconclusive if a required trace or grade is missing.",
        "dispatch_authorized": False,
    }
    return {"proposal": proposal, "candidate": {"diff": patch, "manifest": manifest},
            "experiment_request": experiment}


def publish_proposal(output: Path, result: dict) -> Path:
    finding_id = result["proposal"]["id"]
    require(re.fullmatch(r"p05-[0-9a-f]{64}", finding_id) is not None, "invalid proposal ID")
    path = output / f"{finding_id}.json"
    if path.is_symlink() or path.exists() and not path.is_file():
        raise Conflict("proposal target must be a regular file")
    publish(path, encode(result))
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("product-archive", "codex-archive", "baseline-archive", "output"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--baseline", required=True)
    args = parser.parse_args()
    try:
        output = args.output.resolve(strict=True)
        protected = [
            Path(__file__).resolve().parents[2], args.product_archive.resolve(),
            args.codex_archive.resolve(), args.baseline_archive.resolve().parent,
        ]
        require(output.is_dir() and all(not output.is_relative_to(path) and not path.is_relative_to(output)
                                        for path in protected), "proposal output must be an isolated directory")
        product, codex = ReadOnlyArchive(args.product_archive), ReadOnlyArchive(args.codex_archive)
        evidence = load_pilot(product, codex)
        skill, selector = baseline_inputs(args.baseline_archive, args.baseline)
        replay = reproduce(evidence)
        print(publish_proposal(output, propose(evidence, args.baseline, skill, selector, replay)))
    except (ValueError, OSError, KeyError, IndexError, TypeError, TarError, subprocess.SubprocessError) as error:
        parser.exit(1, f"proposal unavailable ({type(error).__name__})\n")


if __name__ == "__main__":
    main()
