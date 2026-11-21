from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from factory.checkpoint import capture, decode, git
from factory.verify import reconstruct


def main(inputs, base, known):
    with TemporaryDirectory() as directory:
        repo = Path(directory)
        reconstruct(repo, inputs / "known-solution.bundle", base, known)
        entrypoint = repo / "src/factory/verified_patch.py"
        original = entrypoint.read_bytes()
        old = b'print(f"{args.run_id}: corrupt evidence: {error}", file=sys.stderr)'
        if original.count(old) != 1:
            raise ValueError("cannot identify the P03 corruption diagnostic")
        entrypoint.write_bytes(original.replace(old, b'print(f"{args.run_id}: damaged evidence", file=sys.stderr)'))
        git(repo, "add", str(entrypoint))
        git(repo, "-c", "user.name=Factory calibration", "-c", "user.email=calibration@example.invalid",
            "commit", "-m", "Change the verified patch corruption diagnostic",
            env={"GIT_AUTHOR_DATE": "2026-11-21T00:00:00Z", "GIT_COMMITTER_DATE": "2026-11-21T00:00:00Z"})
        mutant = capture(repo, base)
        (inputs / "regression.bundle").write_bytes(decode(mutant["bundle"]))
        print(mutant["head_sha"])


if __name__ == "__main__":
    main(Path(sys.argv[1]), *sys.argv[2:])
