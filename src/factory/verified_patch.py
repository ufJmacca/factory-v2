import argparse
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

from factory.checkpoint import git
from factory.delivery_steps import check_manifest, require
from factory.evidence import Evidence
from factory.verify import Rejected, reconstruct


def verified_patch(store: Evidence, run_id: str) -> bytes:
    manifest = store.read(run_id, "manifest", required=True)
    require(manifest["run_id"] == run_id, "terminal manifest run ID differs")
    require(manifest["producer"] == "records-delivery", "unexpected manifest producer")
    if manifest["status"] == "interrupted":
        raise FileNotFoundError("run did not complete delivery")
    require(manifest["status"] == "delivered", "unexpected terminal status")

    base, head = manifest["base_sha"], manifest["submitted_sha"]
    require(all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (base, head)), "invalid revision")
    step = manifest["recovery"]["checkpoint_step"]
    require(step in ("submission", "green"), "unknown recovery checkpoint step")
    artifacts = manifest["artifacts"]
    missing = manifest["missing_artifacts"]
    checkpoints = {"submission_checkpoint", "green_checkpoint"}
    require(missing.keys() <= checkpoints and not (missing.keys() & artifacts.keys()),
            "inconsistent missing checkpoint records")
    required = {
        "checks", "red_bundle", "red_verdict", "red_checkpoint",
        "submitted_bundle", "verifier_verdict", "intent", "receipt", f"{step}_checkpoint",
    } | (checkpoints - missing.keys())
    if absent := required - artifacts.keys():
        raise FileNotFoundError(f"manifest lacks artifact references: {', '.join(sorted(absent))}")
    blobs = check_manifest(store, manifest)

    with TemporaryDirectory(prefix="verified-patch-") as directory:
        root = Path(directory)
        bundle = root / "submission.bundle"
        bundle.write_bytes(blobs["submitted_bundle"])
        repo = root / "repo"
        repo.mkdir()
        checked = reconstruct(repo, bundle, base, head)
        return git(
            repo, "diff", "--binary", "--no-renames", "--no-ext-diff", "--no-textconv", "--no-color",
            "--src-prefix=a/", "--dst-prefix=b/", base, checked, "--",
            env={"GIT_ATTR_NOSYSTEM": "1"},
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Print the verified Git patch for a completed records delivery.")
    parser.add_argument("--evidence", type=Path, required=True, help="PR 02 evidence archive directory")
    parser.add_argument("run_id")
    args = parser.parse_args()
    try:
        patch = verified_patch(Evidence(args.evidence), args.run_id)
    except FileNotFoundError as error:
        print(f"{args.run_id}: missing evidence: {error}", file=sys.stderr)
        return 1
    except (ValueError, KeyError, TypeError, AttributeError, OSError, Rejected, subprocess.SubprocessError) as error:
        print(f"{args.run_id}: corrupt evidence: {error}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(patch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
