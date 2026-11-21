import argparse
from base64 import b64encode
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import re
import resource
import signal
import subprocess
import sys
import tempfile


SUBMISSION_REF = "refs/heads/submission"
CANDIDATE_UID = 65534
OUTPUT_LIMIT = 64 * 1024
PROCESS_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8"}
GIT_ENV = {
    **PROCESS_ENV,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_TERMINAL_PROMPT": "0",
}


class Rejected(Exception):
    pass


@dataclass(frozen=True)
class Check:
    id: str
    stdin: bytes
    stdout: bytes


@dataclass(frozen=True)
class Output:
    base64: str
    sha256: str

    @classmethod
    def capture(cls, stream):
        stream.seek(0)
        data = stream.read()
        return data, cls(b64encode(data).decode("ascii"), sha256(data).hexdigest())


@dataclass(frozen=True)
class CheckResult:
    id: str
    exit_code: int | None
    passed: bool
    stdout: Output
    stderr: Output


@dataclass
class Verdict:
    base_sha: str
    expected_head: str
    schema_version: int = 1
    checked_sha: str | None = None
    check_digest: str | None = None
    bundle_sha256: str | None = None
    decision: str = "rejected"
    reason: str = ""
    checks: list[CheckResult] = field(default_factory=list)


def load_checks(content: bytes) -> list[Check]:
    try:
        document = json.loads(content)
        if document["schema_version"] != 1:
            raise ValueError
        checks = [
            Check(case["id"], case["stdin"].encode("utf-8"), case["stdout"].encode("utf-8"))
            for case in document["cases"]
        ]
        ids = [check.id for check in checks]
        if not ids or not all(re.fullmatch(r"[a-z0-9_]+", id_) for id_ in ids):
            raise ValueError
        if len(set(ids)) != len(ids):
            raise ValueError
    except (TypeError, AttributeError, KeyError, ValueError) as error:
        raise Rejected("invalid_checks") from error
    return checks


def git(checkout: Path, *args: str, reason: str = "invalid_bundle") -> bytes:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=checkout, env=GIT_ENV, stderr=subprocess.PIPE, timeout=10,
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise Rejected(reason) from error


def reconstruct(checkout: Path, bundle: Path, base: str, head: str) -> str:
    git(checkout, "init", "--quiet", "--template=", "--object-format=sha1")
    refs = git(checkout, "bundle", "list-heads", str(bundle))
    if refs != f"{head} {SUBMISSION_REF}".encode("ascii"):
        raise Rejected("head_mismatch")

    git(
        checkout, "-c", "fetch.fsckObjects=true", "fetch",
        "--no-tags", "--no-recurse-submodules", str(bundle), SUBMISSION_REF,
    )
    fetched = git(checkout, "rev-parse", "--verify", "FETCH_HEAD^{commit}").decode("ascii")
    if fetched != head:
        raise Rejected("head_mismatch")

    base_commit = git(
        checkout, "rev-parse", "--verify", f"{base}^{{commit}}", reason="base_mismatch",
    )
    if base_commit.decode("ascii") != base:
        raise Rejected("base_mismatch")
    git(checkout, "merge-base", "--is-ancestor", base, head, reason="base_mismatch")
    git(checkout, "checkout", "--quiet", "--detach", head)
    checked = git(checkout, "rev-parse", "HEAD").decode("ascii")
    if checked != head:
        raise Rejected("head_mismatch")
    return checked


def limit_output():
    resource.setrlimit(resource.RLIMIT_FSIZE, (OUTPUT_LIMIT, OUTPUT_LIMIT))


def run_check(checkout: Path, check: Check) -> CheckResult:
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        with subprocess.Popen(
            [sys.executable, "-IB", str(checkout / "records.py")],
            cwd=checkout,
            env=PROCESS_ENV,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            user=CANDIDATE_UID,
            group=CANDIDATE_UID,
            extra_groups=[],
            start_new_session=True,
            preexec_fn=limit_output,
        ) as candidate:
            try:
                candidate.communicate(check.stdin, timeout=5)
                exit_code = candidate.returncode
            except subprocess.TimeoutExpired:
                exit_code = None
            finally:
                with suppress(ProcessLookupError):
                    os.killpg(candidate.pid, signal.SIGKILL)
        actual, stdout_evidence = Output.capture(stdout)
        errors, stderr_evidence = Output.capture(stderr)
    passed = exit_code == 0 and actual == check.stdout and not errors
    return CheckResult(check.id, exit_code, passed, stdout_evidence, stderr_evidence)


def verify(*, base: str, head: str, bundle: Path, check_file: Path, check_digest: str) -> Verdict:
    verdict = Verdict(base, head)
    try:
        try:
            content = check_file.read_bytes()
        except OSError as error:
            raise Rejected("invalid_checks") from error
        verdict.check_digest = sha256(content).hexdigest()
        if verdict.check_digest != check_digest:
            raise Rejected("check_digest_mismatch")
        checks = load_checks(content)

        if not all(re.fullmatch(r"[a-f0-9]{40}", sha) for sha in (base, head)):
            raise Rejected("invalid_sha")
        try:
            with bundle.open("rb") as stream:
                verdict.bundle_sha256 = file_digest(stream, "sha256").hexdigest()
        except OSError as error:
            raise Rejected("invalid_bundle") from error

        with tempfile.TemporaryDirectory(prefix="submission-") as directory:
            checkout = Path(directory)
            verdict.checked_sha = reconstruct(checkout, bundle.resolve(), base, head)
            entrypoint = checkout / "records.py"
            if entrypoint.is_symlink() or not entrypoint.is_file():
                raise Rejected("missing_entrypoint")
            checkout.chmod(0o755)
            verdict.checks = [run_check(checkout, check) for check in checks]
    except Rejected as error:
        verdict.reason = str(error)
        return verdict

    failures = [check.id for check in verdict.checks if not check.passed]
    if failures:
        verdict.reason = f"check_failed:{failures[0]}"
    else:
        verdict.decision = "accepted"
        verdict.reason = "checks_passed"
    return verdict


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a records submission with pinned checks.")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--checks", dest="check_file", type=Path, default=Path("/grading/checks.json"))
    parser.add_argument("--check-digest", required=True)
    verdict = verify(**vars(parser.parse_args()))
    print(json.dumps(asdict(verdict), sort_keys=True))
    return int(verdict.decision != "accepted")


if __name__ == "__main__":
    sys.exit(main())
