CASES = ("verified", "corrupt", "missing_artifact", "unknown_run")
CALIBRATION = {
    "known-solution": ("accepted", "all_checks_passed", 1, dict.fromkeys(CASES, True)),
    "unchanged-base": ("rejected", "verified", 0, dict.fromkeys(CASES, False)),
    "regression": ("rejected", "corrupt", 0, {case: case != "corrupt" for case in CASES}),
    "missing-output": ("incomplete_evidence", "missing_submission.bundle", 0, {}),
}


def validate_calibration(name, grade, reward, submitted_sha):
    expected = CALIBRATION[name]
    actual = (grade["status"], grade["reason"], reward, grade["checks"])
    if actual != expected or grade["submitted_sha"] != submitted_sha:
        raise ValueError(f"{name}: expected {expected} at {submitted_sha}, got {grade} with reward {reward}")
