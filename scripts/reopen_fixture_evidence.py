from base64 import b64decode
from hashlib import sha256
import json
from pathlib import Path
import sys


def require(condition: bool, message: str):
    if not condition:
        raise SystemExit(message)


def reopen(directory: Path):
    pinned = (directory / "checks.json").read_bytes()
    digest = sha256(pinned).hexdigest()
    require(digest == (directory / "checks.sha256").read_text().strip(), "Check manifest digest differs")
    cases = json.loads(pinned)["cases"]
    rows = [line.split("\t") for line in (directory / "submissions.tsv").read_text().splitlines()]
    require([row[0] for row in rows] == ["base", "correct", "mutant"], "Expected three fixture submissions")

    output_count = 0
    for name, base, head, expected_reason, expected_status, actual_status in rows:
        require(expected_status == actual_status, f"{name}: unexpected container exit {actual_status}")
        verdict = json.loads((directory / f"{name}.json").read_bytes())
        require(verdict["schema_version"] == 1, f"{name}: unsupported verdict schema")
        require(verdict["base_sha"] == base, f"{name}: wrong base")
        require(verdict["expected_head"] == verdict["checked_sha"] == head, f"{name}: wrong head")
        require(verdict["check_digest"] == digest, f"{name}: check digest differs")
        bundle = (directory / "submissions" / f"{name}.bundle").read_bytes()
        require(verdict["bundle_sha256"] == sha256(bundle).hexdigest(), f"{name}: bundle digest differs")
        require(len(verdict["checks"]) == len(cases), f"{name}: missing check results")

        failures = []
        for case, check in zip(cases, verdict["checks"], strict=True):
            require(check["id"] == case["id"], f"{name}: unexpected check ID")
            outputs = {}
            for channel in ("stdout", "stderr"):
                blob = check[channel]
                output = b64decode(blob["base64"], validate=True)
                require(
                    blob["sha256"] == sha256(output).hexdigest(),
                    f"{name}: {check['id']} {channel} digest differs",
                )
                outputs[channel] = output
                output_count += 1

            passed = (
                check["exit_code"] == 0
                and outputs["stdout"] == case["stdout"].encode("utf-8")
                and outputs["stderr"] == b""
            )
            require(check["passed"] is passed, f"{name}: {check['id']} result differs")
            if not passed:
                failures.append(case["id"])

        decision = "rejected" if failures else "accepted"
        reason = f"check_failed:{failures[0]}" if failures else "checks_passed"
        require(verdict["decision"] == decision, f"{name}: decision differs")
        require(verdict["reason"] == reason == expected_reason, f"{name}: reason differs")
        require(int(actual_status) == int(bool(failures)), f"{name}: exit disagrees with verdict")
        print(f"{name} {decision} {head} {reason}")

    print(f"Reopened three verdicts and verified {output_count} output hashes, three bundle hashes, and the check digest.")


if __name__ == "__main__":
    reopen(Path(sys.argv[1]))
