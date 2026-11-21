# Run a bounded Codex delivery

Use a clean factory checkout and a single trusted host with Git, Docker Compose,
Python 3.12, and Codex CLI 0.162.1. Obtain the maintainer's approval for the
outcome, target base, acceptance check, and run cap before you run the engineer.
The pilot targets a read-only patch retrieval command against the PR 02 merge.
The grader lives in the factory checkout. Pin its digest before launch.

1. Run `make verify-recovery` and retain the path to its `archive/` and the fresh
   run ID from `reopened.txt`. The grader uses this evidence as its input.
2. Set `FACTORY_SHA` to the reviewed factory commit. Set `GRADER_SHA` to the
   approved SHA-256 of `grading/check_verified_patch.py`. Choose a fresh
   `RUN_ID` and an absolute path for `EVIDENCE_DIR`.
3. If you use CLI Proxy, enroll a dedicated key as `factory-delivery` in the
   hub and Langfuse. Set `HUB_URL` to its HTTPS `/v1` endpoint. Set
   `HUB_KEY_FILE` and `HUB_CA_FILE` to the local key and CA files. The key
   file must be private to your host account. The hub manages the upstream
   ChatGPT OAuth login.
4. Run the approved delivery. Replace the illustrative cap and maintainer
   name with the approved values.

```sh
PYTHONPATH=src python3.12 -m factory.live_codex "$RUN_ID" \
    --approved-by "$MAINTAINER" --minutes 30 --attempts 1 \
    --factory-sha "$FACTORY_SHA" --grader grading/check_verified_patch.py \
    --grader-sha "$GRADER_SHA" --evidence "$EVIDENCE_DIR" \
    --fixture "$RECORDS_ARCHIVE" --fixture-run "$RECORDS_RUN_ID" \
    --proxy-url "$HUB_URL" --proxy-key-file "$HUB_KEY_FILE" --proxy-ca "$HUB_CA_FILE"
```

For a direct ChatGPT run, omit the proxy options. Run `codex login status`
first and select a model that the direct subscription advertises in
`agents.toml`. Commit the profile before passing its SHA. The runner checks
the CLI build and verifies model availability on the selected route.

The trusted CLI runs with an empty local directory and a restricted
filesystem profile. Its two preapproved tools are `workspace` and
`milestone`. The workspace tool runs commands in a disposable, offline
Docker checkout. The milestone tool records phase evidence and a checkpoint.
The proxy client keeps its key in the trusted CLI process environment. The
candidate receives no host mounts or credentials. Temporal owns attempt
dispatch, independent verification, and terminal status.

Duplicate dispatch replays the recorded result. After a fenced interruption,
a retry reads the pinned profile and skills and restores the latest checkpoint.
An unresolved host interruption leaves an active marker and the next dispatch
stops with `unfenced_prior_attempt`. Use one evidence root per trusted runner.
Quota exhaustion, lost login, and a rejected proxy key end the run explicitly.
The execution deadline stops new work and ends the model process. Temporal
gives the runner 30 seconds to publish its cleanup receipt and evidence before
finalizing. A missing receipt ends the run with `receipt_timeout`.
Billing remains unknown when the CLI supplies no attributable cost.

The runner removes the product and grading containers. The terminal manifest
remains under `runs/<run-id>/manifest.json`. Find the local native event stream
in `runs/<run-id>/attempt-1.jsonl`. The manifest indexes the content-addressed
copy, the checkpoint, the independent verdict, and the exact submission bundle
under `blobs/<sha256>`. Reopen the manifest after cleanup with a read-only
evidence mount. Set `WORKER_IMAGE` to the image ID in the run's `request.json`.

```sh
docker run --rm --network none --read-only -v "$EVIDENCE_DIR:/retained:ro" \
    "$WORKER_IMAGE" python -m factory.outcome_worker reopen /retained "$RUN_ID"
```

If the grader failed during setup after the engineer submitted, recover the
verification against the saved commit. Use the same records archive. Recovery
does not launch Codex and uses the grader bytes pinned at admission.

```sh
PYTHONPATH=src python3.12 -m factory.reverify_codex "$RUN_ID" \
    --evidence "$EVIDENCE_DIR" --fixture "$RECORDS_ARCHIVE"
```

Recovery writes `runs/<run-id>/verification-recovery.json` and keeps the
original terminal manifest. To check both records, build the current worker
image and use it in the read-only reopen command with
`verification-recovery` after `"$RUN_ID"`.
