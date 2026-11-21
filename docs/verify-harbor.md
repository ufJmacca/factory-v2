# Verify the frozen Harbor task

Run the calibration from a clean checkout on the trusted Docker host. Keep the
P03 archive, records fixture, and held-out files in private host storage.

The command reads `p03`, `records`, and `held-out` under
`~/.local/state/factory-v2/harbor-v1`. Set `HARBOR_INPUT_ROOT` to use another
private directory, or set individual absolute paths below.

| Variable | Directory |
| --- | --- |
| `HARBOR_P03_ARCHIVE` | The P03 `live` archive with `runs/pr03-proxy-final` and `blobs`. |
| `HARBOR_RECORDS_ARCHIVE` | The original PR 02 fixture archive used by that run. |
| `HARBOR_HELD_OUT_DIR` | The private, frozen `rpc-unknown-operation` and `profile-preview` directories. |
| `EVIDENCE_ROOT` | A private directory for the new receipts. Defaults to `.evidence`. |

The Docker daemon must have the original Linux arm64 P03 workspace image
loaded. The command checks its full image ID. If the image is missing, load
the retained image from the operator's archive before starting the run.
Use Docker Compose with the local Docker socket at `/var/run/docker.sock`.

```sh
make verify-harbor
```

The controller validates the archive hashes before starting Harbor. The
[freeze manifest](../evaluation/harbor/frozen.json) pins the approved base,
original grader, fixture, expected patch, workspace image, skills, and inputs
for the separate verifier. The [split manifest](../evaluation/harbor/split.json)
pins both held-out cases. Give each later trial only its own task and base.
Retain the held-out checks in the trusted environment.

The agent starts from the archived PR 02 bundle with a clean worktree and no
remote. Harbor transfers the designated submission bundle and stops the
agent before grading. The agent has an editable checkout, temporary storage,
its own log directory, and a dedicated publication directory. Docker mounts
the grading inputs only in the separate verifier. The verifier runs the
submitted CLI in a filesystem jail under an unprivileged UID. Each case
gets a fresh fixture copy.

The command prints the evidence directory. Read `summary.json` for the tested
commit, task and grader digests, resource counts, and four trial IDs.
Harbor stores its native result and artifact manifest under `trials/`.
The summary records these expected verdicts.

| Submission | Verdict | Reason |
| --- | --- | --- |
| Accepted P03 commit | Accepted | The byte-exact patch and all three error cases pass. |
| Original PR 02 base | Rejected | The required CLI fails the successful patch case. |
| Accepted P03 commit with a changed diagnostic | Rejected | The successful patch case passes. The corruption diagnostic fails. |
| Omitted submission bundle | Incomplete evidence | The required bundle is missing. The receipt has no submitted SHA. |

Harbor uses a numeric reward of zero for the last case. Use the structured
receipt to identify incomplete evidence. Keep the native results and
protected staging files on the trusted host.
