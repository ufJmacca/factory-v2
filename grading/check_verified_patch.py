"""Protected acceptance check for the verified patch CLI pilot."""
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import tarfile

from factory import checkpoint
from factory.codex_workspace import Workspace, docker
from factory.delivery_steps import reopen
from factory.evidence import Evidence, digest


def acceptance(request, snapshot, fixture_root, fixture_run):
    control = Evidence(fixture_root)
    manifest = reopen(control, fixture_run)
    control_checkpoint = control.read_blob(manifest["artifacts"]["submission_checkpoint"])
    with TemporaryDirectory() as directory:
        import json
        expected_repo = Path(directory) / "expected"
        checkpoint.restore(expected_repo, json.loads(control_checkpoint))
        patch = checkpoint.git(
            expected_repo, "diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames",
            manifest["base_sha"], manifest["submitted_sha"],
        )
    blob_ref = manifest["artifacts"]["submitted_bundle"]
    verdicts = []
    for case in ("verified", "corrupt", "missing_artifact", "unknown_run"):
        archive = BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for path in sorted(fixture_root.rglob("*")):
                relative = path.relative_to(fixture_root)
                if not path.is_file() or path.name == ".lock":
                    continue
                if path.name == blob_ref and case == "missing_artifact":
                    continue
                info = tar.gettarinfo(str(path), arcname=str(relative))
                data = path.read_bytes()
                if path.name == blob_ref and case == "corrupt":
                    data = bytes([data[0] ^ 1]) + data[1:]
                info.mode = 0o644
                tar.addfile(info, BytesIO(data))
        workspace = Workspace(f"factory-grade-{request['run_id']}-{case.replace('_', '-')}")
        with workspace.provision(request["workspace_image"], snapshot):
            docker("exec", "-i", workspace.name, "sh", "-ec",
                   "mkdir /tmp/archive && tar -xf - -C /tmp/archive", input=archive.getvalue())
            state = ["sh", "-ec", "find /tmp/archive -type f -print0 | sort -z | xargs -0 sha256sum"]
            before = workspace.run(state, limit=256000)
            arg = "absent-run" if case == "unknown_run" else fixture_run
            actual = workspace.run([
                "env", "PYTHONPATH=src", "PYTHONDONTWRITEBYTECODE=1", "python",
                "-m", "factory.verified_patch", "--evidence", "/tmp/archive", arg,
            ])
            after = workspace.run(state, limit=256000)
            unchanged = before == after and before["exit_code"] == 0 and not before["truncated"]
            product_check = (
                actual["exit_code"] == 0 and actual["output"].encode() == patch
                if case == "verified" else actual["exit_code"] != 0 and
                ("corrupt" if case == "corrupt" else "missing") in actual["output"].casefold()
            )
            verdicts.append({"case": case, "passed": unchanged and product_check and not actual["truncated"],
                             "archive_unchanged": unchanged, "exit_code": actual["exit_code"],
                             "output_sha256": digest(actual["output"].encode())})
    return {
        "schema_version": 1, "checked_sha": snapshot["head_sha"],
        "grader_digest": request["grader_digest"], "expected_patch_sha256": digest(patch),
        "decision": "accepted" if all(item["passed"] for item in verdicts) else "rejected",
        "checks": verdicts,
    }
