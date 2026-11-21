# Compare the Phase 05 delivery skill

Run the scripted comparison from a clean checkout on the trusted Docker host.

~~~sh
make verify-harbor-experiment
~~~

Use the pinned CLI and private Phase 06 inputs documented in [Verify the frozen
Harbor task](../verify-harbor.md). The comparison also reads
`PHASE08_CONTROLS`, which defaults to `phase08-controls` under the same
private input root. Each held-out task directory contains `base.bundle`,
`known.bundle`, `regression.bundle`, and `pin.json`. The pin records the SHA
and bundle digest for each control. The held-out contract must accept the
known revision and reject both controls at their exact SHAs before dispatch.

The command prints the private run directory. Open `experiment.json` for the
frozen inputs and `summary.json` for the assignment and attempt ledger.
Under `runs/`, the assignment ID binds the Harbor intent, job, rollout, and
Temporal run. The `eval-<rollout>` records bind the exported submission,
native events, usage, grade, provider stop, and fixture receipt. The
`blobs/` directory stores those artifacts by SHA-256.

If the supervisor exits early, retain its Compose project and use the printed
`PHASE08_RESUME` path from the same commit. An intent with a lost submit reply
reuses its Harbor idempotency key. To halt the batch, create `stop` in the
private run directory while the supervisors are still running. The controller
reconciles active work and accounts for the remaining assignments.

## Run the approved subscription comparison

Obtain fresh approval for the live envelope before starting. It covers 18
assignments, one evaluation at a time, zero retries, and a four-hour batch.
Each Codex attempt has a ten-minute deadline. The agent has one CPU and
512 MiB of RAM. The separate verifier has one CPU and 768 MiB. The
`factory-delivery` subscription uses the Phase 03 HTTPS CLI Proxy Hub.
API fallback is disabled. The product Temporal queue remains reserved.
Quota exhaustion stops new evaluation assignments.

~~~sh
PHASE08_MODE=live \
PHASE08_APPROVED_BY="$MAINTAINER" \
BRIDGE_HUB_URL="$HUB_URL" \
BRIDGE_HUB_KEY_FILE="$HUB_KEY_FILE" \
BRIDGE_HUB_CA_FILE="$HUB_CA_FILE" \
make verify-harbor-experiment
~~~

## Review the recorded decision

- [Frozen live experiment](live-frozen-experiment.json)
- [Live assignment ledger](live-assignment-ledger.json)
- [Live historical and held-out pairs](live-paired-results.json)
- [Decision record](decision.md)

The scripted [plan](frozen-experiment.json), [ledger](assignment-ledger.json),
and [paired results](paired-results.json) record the preflight comparison.

The committed records contain identifiers, digests, and counts. The full
records, credentials, traces, and protected contract inputs are on the
trusted host.
