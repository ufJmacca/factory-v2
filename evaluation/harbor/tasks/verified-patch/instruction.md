Add one read-only CLI for an operator to retrieve the verified Git patch for a
PR 02 records-delivery run ID. Implement the interface:
  PYTHONPATH=src python -m factory.verified_patch --evidence ARCHIVE RUN_ID
For a completed accepted delivery, emit the byte-exact binary-capable Git diff
from its recorded base to its verified submission, with standard Git paths
and no rename detection. Read the existing PR 02 archive format. Recheck
evidence integrity, verdict, and checked revision before emitting any patch.
Return nonzero with a clear stderr diagnostic containing 'corrupt' for corrupt
evidence, or 'missing' for a missing run or required artifact. Leave the evidence
archive untouched. Add focused behavioral tests and minimal usage documentation.
The target starts at the PR 02 merge. The independent grader is outside your
workspace. You may use the existing records fixture to construct your own tests.

The repository is at `/work/product`. Work there and commit your changes.
Write a self-contained Git bundle of your submitted commit to
`/output/submission.bundle` using the single ref `refs/heads/submission`.

```sh
git update-ref refs/heads/submission HEAD
git bundle create /output/submission.bundle refs/heads/submission
```
