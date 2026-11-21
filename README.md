# Verify a submitted revision

Run `make verify-fixtures` from the factory checkout with Git, Make, Docker Compose, and either `sha256sum` or `shasum` installed. The command builds the images, runs the tests, generates three Git bundles, and verifies each bundle in its own container.

| Submission | Expected verdict | Reason |
| --- | --- | --- |
| Base | Rejected | `check_failed:final_record_without_newline` |
| Correct | Accepted | `checks_passed` |
| Mutant | Rejected | `check_failed:empty_input` |

The command prints a fresh evidence directory under `.evidence/`. Set `EVIDENCE_ROOT` to retain evidence outside the checkout. Each directory holds the three verdicts and their raw stdout and stderr as base64 with SHA-256 hashes, the submitted bundles, and the exact checks. `run.txt` records the images, containers, and elapsed fixture time after the build. `test.log` records the test results.

The fixture driver records the expected SHA from each Git commit before handing its bundle to the verifier. After the verifier containers and their temporary checkouts are gone, a separate `workspace` container reopens the evidence. It recomputes the check, bundle, and output hashes and checks the verdicts against the pinned outputs. To repeat that step, set `EVIDENCE_DIR` to the printed absolute path and run:

```sh
docker compose run --rm --no-deps -T -v "$EVIDENCE_DIR:/evidence:ro" workspace \
    uv run --locked --offline python scripts/reopen_fixture_evidence.py /evidence
```

The image build can fetch base images and Debian packages. The fixture commands run with networking disabled, make zero model calls, and write evidence only to local disk.

## Verify one bundle

Supply a full SHA-1 Git bundle that advertises `refs/heads/submission`, the base and expected head commit SHAs, and the SHA-256 of the trusted `grading/checks.json`. Set `BUNDLE` to the absolute bundle path before running:

```sh
docker compose build verifier
docker compose run --rm --no-deps -T -v "$BUNDLE:/submission.bundle:ro" verifier \
    --base "$BASE_SHA" --head "$HEAD_SHA" --bundle /submission.bundle \
    --check-digest "$CHECK_DIGEST"
```

The verifier checks ancestry, detaches at the advertised commit, and runs `records.py` as an unprivileged user. The verifier emits one JSON verdict on stdout. Exit 0 means accepted. Exit 1 means rejected. The fixture driver requires both the expected exit status and valid evidence.

## Recover one delivery

Run `make verify-recovery` from a clean checkout with Git, Make, and Docker Compose. The command builds the images, runs the workspace tests, and starts a dedicated Compose project. Docker may fetch images and dependencies during the build.

1. The scripted contributor submits the records fixture. A separate process runs the one-shot verifier against the Git bundle.
2. The worker records the fixture intent. The fixture commits the effect to its SQLite ledger, then a failpoint kills the worker before it can complete the Temporal activity.
3. The command removes the worker and candidate containers. The replacement worker connects to the same Temporal database, restores the verified checkpoint into a fresh checkout, and attaches the receipt from the fixture ledger.
4. The command submits the same fixture under a fresh run ID. It removes both checkouts and reopens both terminal manifests and their artifacts using a read-only mount.

If the submission checkpoint is missing, the worker restores the earlier verified green checkpoint. If both are missing, it marks the run interrupted and retains any matching receipt for an effect that already happened.

The candidate has a disposable checkout under `/tmp`. The worker and the candidate use a local Unix socket to transfer checkpoint data. The candidate cannot mount the evidence volume or reach a network. The proof uses zero model calls and applies one local fixture effect per delivery.

The command prints the evidence directory under `.evidence/`. Set `EVIDENCE_ROOT` to keep exports outside the checkout. In the evidence directory:

| Path | Contents |
| --- | --- |
| `archive/runs/<run-id>/manifest.json` | Terminal status, submitted SHA, verdict, recovery checkpoint, Temporal IDs, and artifact hashes |
| `archive/runs/<run-id>/intent.json`, `receipt.json` | Fixture request with its stable operation ID and the matching receipt |
| `archive/blobs/<sha256>` | Git bundles, checkpoints, checks, raw verdicts, intents, and receipts |
| `before/runs/<crash-run-id>/` | Retained run state after the worker died, before it attached the receipt |
| `fixture-before-restart.json`, `fixture-after.json` | Fixture ledger and apply counts before and after recovery |
| `test.log`, `run.txt`, `reopened.txt` | Test output, elapsed time, container count, workflow IDs, run IDs, and effect counts |

Compose stops the containers after the drill and retains the named volumes. The volume names for Temporal history, delivery evidence, and the fixture ledger appear in `run.txt`.

## Follow a product PR through review

Run `make verify-product` to reproduce PR reconciliation, approval deadlines,
and fixture deployment. See the [product delivery guide](docs/collect-product-delivery.md)
for live authorization, exact-revision review, and evidence recovery.

## Run one Codex engineer

Run `make verify-codex` with the same Docker tooling, Python 3.12 on the host,
and Codex CLI 0.162.1. The command runs the regression tests in Compose. It
also drives the installed CLI against a local scripted endpoint, records red
and green, and reopens the submitted revision in a fresh offline container.
Set `FACTORY_HOST_PYTHON` or `FACTORY_CODEX_CLI` to select an installed binary.
The check makes zero subscription model calls.

The [live delivery guide](docs/collect-codex-delivery.md) covers the
maintainer-approved subscription run and its evidence.

## Retrieve a verified patch

From the factory checkout, pass the exported PR 02 `archive` directory (the one containing
`runs/` and `blobs/`) and the run ID:

```sh
PYTHONPATH=src python -m factory.verified_patch --evidence "$EVIDENCE_DIR/archive" "$RUN_ID" > verified.patch
```

The command leaves the archive untouched and writes the binary-capable Git diff to stdout only after checking the evidence for a completed, accepted delivery. Missing or corrupt evidence produces a diagnostic on stderr and a nonzero exit status.
