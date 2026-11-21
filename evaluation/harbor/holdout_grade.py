"""Run one frozen held-out contract in the offline separate verifier."""
from contextlib import suppress
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile

import historical_grade as harbor
from verify import Rejected, SUBMISSION_REF, git, limit_output, reconstruct


def check_contract(checkout):
    program = (
        "import sys; sys.path.insert(0, '/reference/src'); "
        "exec(compile(sys.stdin.buffer.read(), '<contract>', 'exec'), globals())"
    )
    uid = 61000
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/tmp", "TMPDIR": "/tmp",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "PYTHONPATH": "/" + str(checkout.relative_to(harbor.JAIL) / "src"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    checks = (harbor.TESTS / "checks.py").read_bytes()
    with tempfile.TemporaryFile() as output:
        with subprocess.Popen(
            harbor.jailed(uid, sys.executable, "-IB", "-c", program), env=env, stdin=subprocess.PIPE,
            stdout=output, stderr=output, start_new_session=True, preexec_fn=limit_output,
        ) as process:
            try:
                process.communicate(input=checks, timeout=35)
                code = process.returncode
            except subprocess.TimeoutExpired:
                code = None
            finally:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        output.seek(0)
        data = output.read()
    (harbor.LOGS / "contract.log").write_bytes(data)
    if code not in (0, 1) or re.search(rb"(?m)^Ran 1 test in ", data) is None:
        return None
    return code == 0


def grade(config):
    result = {"status": "rejected", "reason": "contract", "submitted_sha": None,
              "bundle_sha256": None, "checks": {}, "grader_digest": config["grader_digest"]}
    bundle = harbor.BUNDLE
    if not bundle.exists():
        return {**result, "status": "incomplete_evidence", "reason": "missing_submission.bundle"}
    info = bundle.lstat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 16 * 1024**2:
        return {**result, "reason": "invalid_submission.bundle"}
    result["bundle_sha256"] = harbor.sha256(bundle.read_bytes()).hexdigest()
    jail = harbor.JAIL
    subprocess.run(["cp", "-a", "/bin", "/etc", "/lib", "/opt", "/usr", str(jail)], check=True)
    (jail / "tmp").mkdir(mode=0o1777)
    (jail / "tmp").chmod(0o1777)
    (jail / "dev").mkdir()
    for name, mode, device in (("null", 0o666, 3), ("urandom", 0o444, 9)):
        path = jail / "dev" / name
        os.mknod(path, stat.S_IFCHR | mode, os.makedev(1, device))
        path.chmod(mode)
    shutil.copytree(harbor.TESTS / "reference", jail / "reference")
    try:
        with tempfile.TemporaryDirectory(dir=jail, prefix="checkout-") as folder:
            checkout = Path(folder)
            heads = git(checkout, "bundle", "list-heads", str(bundle)).decode("ascii")
            match = re.fullmatch(r"([0-9a-f]{40}) " + re.escape(SUBMISSION_REF), heads)
            if match is None:
                raise Rejected("invalid_submission_ref")
            result["submitted_sha"] = reconstruct(checkout, bundle, config["base_sha"], match[1])
            checkout.chmod(0o755)
            passed = check_contract(checkout)
    except (Rejected, UnicodeError) as error:
        return {**result, "reason": str(error)}
    if passed is None:
        return {**result, "status": "incomplete_evidence", "reason": "held_out_check_did_not_finish"}
    return {**result, "checks": {"contract": passed}, "status": "accepted" if passed else "rejected",
            "reason": "all_checks_passed" if passed else "contract"}


if __name__ == "__main__":
    harbor.grade = grade
    harbor.main()
