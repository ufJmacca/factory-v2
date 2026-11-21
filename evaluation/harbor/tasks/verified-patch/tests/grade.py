from contextlib import suppress
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile

from verify import Rejected, SUBMISSION_REF, git, limit_output, reconstruct


TESTS = Path("/tests")
LOGS = Path("/logs/verifier")
BUNDLE = Path("/output/submission.bundle")
JAIL = Path("/jail")
CASES = ("verified", "corrupt", "missing_artifact", "unknown_run")


def archive_state(root):
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise ValueError("archive root is not a directory")
    state = {}
    for path in (root, *root.rglob("*")):
        info = path.lstat()
        kind = stat.S_IFMT(info.st_mode)
        if kind not in (stat.S_IFREG, stat.S_IFDIR):
            raise ValueError("archive contains a nonregular entry")
        content = sha256(path.read_bytes()).hexdigest() if kind == stat.S_IFREG else None
        state[path.relative_to(root)] = (info.st_ino, info.st_mode, info.st_mtime_ns, content)
    return state


def jailed(uid, *args):
    return ["/usr/sbin/chroot", f"--userspec={uid}:{uid}", "--groups=", str(JAIL), *args]


def grade_case(config, checkout, case, uid):
    with tempfile.TemporaryDirectory(dir=JAIL / "tmp") as directory:
        scratch = Path(directory)
        os.chown(scratch, uid, uid)
        archive = scratch / "archive"
        shutil.copytree(TESTS / "archive", archive)
        blob = archive / "blobs" / config["records_bundle_sha256"]
        if case == "missing_artifact":
            blob.unlink()
        if case == "corrupt":
            data = blob.read_bytes()
            blob.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        for path in (archive, *archive.rglob("*")):
            os.chown(path, uid, uid)
        before = archive_state(archive)

        run = "absent-run" if case == "unknown_run" else config["records_run"]
        home = "/" + str(scratch.relative_to(JAIL))
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": home, "TMPDIR": home,
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": "src"}
        subprocess.run(
            jailed(uid, "/bin/sh", "-ec",
                   'test "$(id -u)" = "$1" && test ! -e /tests && test ! -e /logs '
                   '&& test ! -e /proc && test ! -e /var/run/docker.sock', "probe", str(uid)),
            check=True, env=env,
        )
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            with subprocess.Popen(
                jailed(uid, "/bin/sh", "-ec", 'cd "$1"; shift; exec "$@"', "submission",
                       "/" + str(checkout.relative_to(JAIL)), sys.executable, "-B", "-m",
                       "factory.verified_patch", "--evidence", home + "/archive", run),
                env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                start_new_session=True, preexec_fn=limit_output,
            ) as process:
                try:
                    code = process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    code = None
                finally:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
            stdout.seek(0)
            stderr.seek(0)
            output, errors = stdout.read(), stderr.read()

        try:
            unchanged = before == archive_state(archive)
        except (OSError, ValueError):
            unchanged = False
        output_digest = sha256(output).hexdigest()
        if case == "verified":
            passed = code == 0 and output_digest == config["expected_patch_sha256"]
        else:
            diagnostic = b"corrupt" if case == "corrupt" else b"missing"
            passed = code not in (None, 0) and not output and diagnostic in errors.lower()
        (LOGS / f"{case}.stderr").write_bytes(errors)
        return {
            "passed": unchanged and passed, "archive_unchanged": unchanged, "exit_code": code,
            "stdout_sha256": output_digest, "stderr_sha256": sha256(errors).hexdigest(),
        }


def grade(config):
    result = {"status": "rejected", "submitted_sha": None, "bundle_sha256": None, "checks": {}}
    try:
        info = BUNDLE.lstat()
    except FileNotFoundError:
        return {**result, "status": "incomplete_evidence", "reason": "missing_submission.bundle"}
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 16 * 1024**2:
        return {**result, "reason": "invalid_submission.bundle"}
    result["bundle_sha256"] = sha256(BUNDLE.read_bytes()).hexdigest()
    subprocess.run(["cp", "-a", "/bin", "/etc", "/lib", "/opt", "/usr", str(JAIL)], check=True)
    (JAIL / "tmp").mkdir()
    (JAIL / "dev").mkdir()
    os.mknod(JAIL / "dev/null", stat.S_IFCHR | 0o666, os.makedev(1, 3))
    (JAIL / "dev/null").chmod(0o666)
    os.mknod(JAIL / "dev/urandom", stat.S_IFCHR | 0o444, os.makedev(1, 9))
    try:
        with tempfile.TemporaryDirectory(dir=JAIL, prefix="checkout-") as directory:
            checkout = Path(directory)
            heads = git(checkout, "bundle", "list-heads", str(BUNDLE)).decode("ascii")
            match = re.fullmatch(r"([0-9a-f]{40}) " + re.escape(SUBMISSION_REF), heads)
            if match is None:
                raise Rejected("invalid_submission_ref")
            result["submitted_sha"] = reconstruct(checkout, BUNDLE, config["base_sha"], match[1])
            checkout.chmod(0o755)
            details = {case: grade_case(config, checkout, case, uid) for uid, case in enumerate(CASES, 61000)}
            result["checks"] = {case: check["passed"] for case, check in details.items()}
            result["check_details"] = details
    except (Rejected, UnicodeError) as error:
        return {**result, "reason": str(error)}
    for case, passed in result["checks"].items():
        if not passed:
            return {**result, "reason": case}
    return {**result, "status": "accepted", "reason": "all_checks_passed"}


def main():
    os.umask(0o022)
    active_interfaces = [
        name for _, name in socket.if_nameindex()
        if int(Path(f"/sys/class/net/{name}/flags").read_text(), 16) & 1
    ]
    isolation = {
        "container": socket.gethostname(),
        "root_read_only": bool(os.statvfs("/").f_flag & os.ST_RDONLY),
        "tests_read_only": bool(os.statvfs(TESTS).f_flag & os.ST_RDONLY),
        "agent_workspace_absent": not Path("/work/product").exists(),
        "agent_marker_absent": not Path("/tmp/agent-only").exists(),
        "docker_socket_absent": not Path("/var/run/docker.sock").exists(),
        "loopback_only": active_interfaces == ["lo"],
    }
    if not all(isolation.values()):
        raise RuntimeError(f"verifier isolation check failed: {isolation}")
    config = json.loads((TESTS / "expected.json").read_bytes())
    result = grade(config)
    (LOGS / "grade.json").write_text(json.dumps({**result, "isolation": isolation}) + "\n")
    (LOGS / "reward.txt").write_text("1\n" if result["status"] == "accepted" else "0\n")


if __name__ == "__main__":
    main()
