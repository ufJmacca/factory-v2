from collections import Counter

from factory.evidence import Conflict
from factory.harbor_experiment import ARMS, LIVE_ENVELOPE, TUNING, plan_digest


PILOT_ID = "p09-fresh-paired-pilot"
RED_ARGV_PILOT_ID = "p09-red-argv-pilot"
PILOT_ENVELOPE = {**LIVE_ENVELOPE, "new_paid_infrastructure_usd": 0}


def task_instruction(prompt):
    return prompt + "\n\nUse the assigned workspace tool for repository operations.\n"


def require_red_argv_isolation(arms):
    if (set(arms) != set(ARMS) or arms["baseline"].get("red_argv_guidance") is not False
            or arms["candidate"].get("red_argv_guidance") is not True
            or {**arms["baseline"], "red_argv_guidance": True} != arms["candidate"]):
        raise Conflict("revised pilot arms must differ only in red argv guidance")


def assignment_rows(design):
    try:
        rows = design["schedule"]["assignments"]
        envelope = design["authorized_envelope"]
        held_out = design["tasks"]["held_out"]
        held_out_ids = {task["id"] for task in held_out}
        repositories = {task["repository"].casefold() for task in held_out}
        prefixes = {PILOT_ID: "p09", RED_ARGV_PILOT_ID: "r09"}
        prefix = prefixes.get(design["id"])
        if (prefix is None or design["tasks"]["historical"]["id"] != TUNING
                or len(rows) != PILOT_ENVELOPE["max_assignments"]
                or len(held_out) != 8 or len(held_out_ids) != 8 or len(repositories) != 8
                or TUNING in held_out_ids or any(envelope[key] != value for key, value in PILOT_ENVELOPE.items())):
            raise Conflict("fresh pilot design exceeds the approved task or runtime envelope")
        if [row["id"] for row in rows] != [f"{prefix}-{i:02d}" for i in range(1, len(rows) + 1)]:
            raise Conflict("fresh pilot assignment identities differ from the sealed order")
        pairs = list(zip(rows[::2], rows[1::2]))
        first_tasks = [first["task"] for first, _ in pairs]
        if first_tasks[0] != TUNING or set(first_tasks[1:]) != held_out_ids:
            raise Conflict("fresh pilot must run one historical pair and one pair per held-out task")
        if Counter(first["arm"] for first, _ in pairs[1:]) != {"baseline": 4, "candidate": 4}:
            raise Conflict("fresh pilot must balance the first arm across held-out pairs")
        for first, second in pairs:
            split = "historical" if first["task"] == TUNING else "held_out"
            if (first["task"] != second["task"] or first["split"] != split or second["split"] != split
                    or {first["arm"], second["arm"]} != set(ARMS)):
                raise Conflict("fresh pilot assignment is outside its frozen pair")
    except (AttributeError, IndexError, KeyError, TypeError) as error:
        raise Conflict("fresh pilot design is malformed") from error
    return [{**row, "pair": 1} for row in rows]


def require_approval(manifest, approval):
    if (manifest["mode"] != "live" or approval.get("digest") != plan_digest(manifest)
            or approval.get("design_sha256") != manifest["design_sha256"]
            or approval.get("envelope") != manifest["envelope"]
            or not isinstance(approval.get("approved_by"), str) or not approval["approved_by"].strip()):
        raise Conflict("live approval must bind the fresh design, execution lock, and envelope")
