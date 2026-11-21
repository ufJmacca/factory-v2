# Review one proposed correction

Run this command on the trusted delivery host from a clean checkout. Set the variables to the retained P04 and P03 `live` directories, each containing `runs/` and `blobs/`.

```sh
P05_PRODUCT_ARCHIVE=/path/to/p04/live \
P05_CODEX_ARCHIVE=/path/to/p03/live \
make verify-proposal
```

The command prints its local evidence directory. It reopens both archives in the offline Compose workspace, restores the approved PR 02 checkpoint, and runs the same requests through the current factory workspace bridge. The bridge rejects the first call before command execution. The retry exits zero. The command ingests the same evidence twice, compares the proposal bytes, checks the patch against the pinned baseline, and compares the protected inputs before and after. It also verifies that the four checked-in artifacts match the generated files. The record links use SHA-256 over compact, sorted JSON with a trailing newline.

| Artifact | Purpose |
| --- | --- |
| [Proposal](proposal.json) | Diagnosis, source pointers, successful comparison, and review state |
| [Candidate diff](candidate.patch) | One addition to the `delivery-evidence` skill |
| [Candidate manifest](candidate-manifest.json) | Baseline commit and selector, candidate skill digest, and patch digest |
| [Experiment request](experiment-request.json) | Frozen inputs, changed skill, prediction, and disproof condition |

## Follow the source events

The P03 run is `pr03-proxy-final`. Read one-based lines 19 (`item_9`) and 21 (`item_10`) of `attempt-1.jsonl` on the trusted host. The terminal manifest binds the stream to SHA-256 `d5329eccdb036c4da69778a880db948d34df0023899aaf03bb45f61b9b99f987`. The first request supplied `a` alongside `command` and got `invalid workspace command`. The second request sent the same `command` as its only field and succeeded. Lines 11 and 13, and lines 15 and 17, show the same pattern on two other reads.

The archived configuration resolves `delivery-evidence` and its bytes appear in the reconstructed prompt. The native events leave model context and the origin of the extra field unknown. The experiment tests whether a schema instruction reduces this failure. It counts rejected initial workspace calls across three fresh pairs on the historical task. The candidate must reduce the count by at least one and pass the frozen grader on each exact submission. An equal or higher count disproves the prediction. A missing trace or grade yields an inconclusive result. The experiment request is pending review. Activation requires a separate decision.

## Reopen the P04 leads

The table lists the retained records under `runs/` in the P04 archive. The SHA-256 values cover the canonical manifest bytes.

| Run | Manifest | Accepted revision | Manifest SHA-256 |
| --- | --- | --- | --- |
| `pr03-proxy-final` | `product-manifest.json` | `30ad19025943db2134ca58d9b1eb51510c254796` | `b6b0e1e8c866bc023073edda28eaf3f635dfda5df1489cc704c0182b5e385bdd` |
| `pr03-proxy-final-integration` | `product-manifest.json` | `27750af1639922f15b18648d77bf913f3be17069` | `d0543ebe3b855f162524ea4e436950e14cf05b0601b91961f73b0098a66a78ea` |
| `pr03-proxy-final-attribution` | `attribution-manifest.json` | `21d1f301804cfae1c01ab46ee84772e9a25b201c` | `47ebdecb418ec9f286ff6a61536d0781331692012234376ca99a6cfaa68abf51` |

P03's `request.json` deliberately pins PR 02 at `31f67b2ea40d4fa4570b26dd9b175e9fe423f416`. Events 57 and 59 show the product documentation edit. The following command reproduces the conflict P04 recorded in the original manifest at `/feedback/0`. Both branches inserted a README section after the same final line.

```sh
git merge-tree --write-tree --name-only b7ef08dd1dc77f5b2a6ae01e3eecdd59a22a4be0 30ad19025943db2134ca58d9b1eb51510c254796
```

P04's integration manifest records the maintainer's separate attribution approval at `/feedback/0`. The restored integration and attribution Git revisions have the same tree and ordered parents. The attribution record binds the new author and the approved old and new heads to the same PR. These two leads support no diagnosis against a skill or selector.
