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
