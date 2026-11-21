from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from factory.checkpoint import decode
from factory.local_rpc import serve


def verify_bundle(base: str, head: str, bundle: str, check_digest: str) -> dict:
    with TemporaryDirectory() as directory:
        local_bundle = Path(directory) / "submission.bundle"
        local_bundle.write_bytes(decode(bundle))
        process = subprocess.run(
            [
                sys.executable, "-IB", "/grading/verify.py", "--base", base, "--head", head,
                "--bundle", str(local_bundle), "--check-digest", check_digest,
            ],
            capture_output=True, timeout=19,
        )
        if process.returncode not in (0, 1) or not process.stdout:
            raise OSError(f"verifier process exited {process.returncode}: {process.stderr.decode()}")
        return {"exit_code": process.returncode, "verdict": process.stdout.decode()}


def main():
    service = sys.argv[1]
    if service == "candidate":
        from factory.records_workspace import Candidate

        candidate = Candidate(Path("/tmp/checkouts"))
        routes = {"start": candidate.start, "improve": candidate.improve, "restore": candidate.restore}
    elif service == "fixture":
        from factory.fixture import Ledger

        ledger = Ledger(Path("/ledger/effects.sqlite"))
        routes = {"apply": ledger.apply, "lookup": ledger.lookup, "inspect": ledger.inspect}
    elif service == "verifier":
        routes = {"verify": verify_bundle}
    else:
        raise SystemExit(f"unknown service: {service}")
    serve(Path("/rpc/service.sock"), routes)


if __name__ == "__main__":
    main()
