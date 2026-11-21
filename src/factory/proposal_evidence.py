from dataclasses import dataclass
import json
from pathlib import Path
import re

from factory.evidence import Conflict, Evidence


class ReadOnlyArchive:
    def __init__(self, root: Path):
        self._store = Evidence(root)

    def read(self, run_id: str, name: str, *, required: bool = False):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise Conflict("invalid archive record name")
        return self._store.read(run_id, name, required=required)

    def read_blob(self, reference: str) -> bytes:
        return self._store.read_blob(reference)


@dataclass(frozen=True, kw_only=True)
class Observation:
    required: bool | None = None
    present: bool | None = None
    resolved: bool | None = None
    loaded: bool | None = None
    followed: bool | None = None
    instruction_correct: bool | None = None
    tool_failed: bool | None = None
    outcome_defined: bool | None = None
    success: bool | None = None


def diagnose(observation: Observation) -> str:
    o = observation
    loaded_skill = all(value is True for value in (o.required, o.present, o.resolved, o.loaded))
    causes = [
        name for name, observed in (
            ("missing_skill", o.required is True and o.present is False),
            ("unloaded_skill", o.required is True and o.present is True and o.loaded is False),
            ("skipped_instruction", loaded_skill and o.followed is False and o.instruction_correct is True),
            ("incorrect_instruction", loaded_skill and o.followed is True and o.instruction_correct is False),
            ("tool_failure", o.tool_failed is True),
            ("underspecified_outcome", o.outcome_defined is False),
        ) if observed
    ]
    if o.success is True:
        return "unknown" if causes else "success"
    if (o.present is False and (o.resolved is True or o.loaded is True)
            or o.resolved is False and o.loaded is True
            or o.loaded is False and o.followed is True):
        return "unknown"
    return causes[0] if len(causes) == 1 else "unknown"


READ_COMMAND = "cat tests/test_recovery.py"
REJECTED = "invalid workspace command"


@dataclass(frozen=True)
class WorkspaceCall:
    line: int
    item_id: str
    arguments: dict


def workspace_retry(events: bytes, rejected_line: int, retry_line: int) -> tuple[WorkspaceCall, WorkspaceCall] | None:
    lines = events.splitlines()
    if not 1 <= rejected_line < retry_line <= len(lines):
        return None
    failed, retry = (json.loads(lines[line - 1]) for line in (rejected_line, retry_line))
    for event in (failed, retry):
        if event["type"] != "item.completed":
            return None
        item = event["item"]
        if (item["type"], item["server"], item["tool"]) != ("mcp_tool_call", "factory", "workspace"):
            return None
    wrong, right = failed["item"], retry["item"]
    expected = {"command": READ_COMMAND}
    if (wrong["id"] == right["id"] or right["arguments"] != expected
            or wrong["arguments"] != {**expected, "a": 1}):
        return None
    rejected, accepted = (item["result"]["content"][0]["text"] for item in (wrong, right))
    if rejected != REJECTED or right.get("error") is not None:
        return None
    result = json.loads(accepted)
    if result.get("exit_code") != 0 or result.get("truncated") is not False:
        return None
    return (
        WorkspaceCall(rejected_line, wrong["id"], wrong["arguments"]),
        WorkspaceCall(retry_line, right["id"], right["arguments"]),
    )
