# Verify the Harbor delivery bridge

Run the checks on the trusted Docker host from a clean factory checkout. Use
the private Phase 06 archives and retained workspace image documented in
[Verify the frozen Harbor task](verify-harbor.md). Install the pinned Codex CLI
on the host. Compose builds the existing pinned Harbor image and starts its
server on the internal recovery network.

```sh
make verify-harbor-bridge
```

The scripted check drives the installed CLI with a local Responses fixture.
It submits jobs through Harbor's HTTP API and occupies both evaluation slots
with running workspace commands. The product runner starts on its own Temporal
queue. The check cancels a queued trial and cancels active trials from Harbor
and Temporal. It pauses the Harbor process to expire a lease and checks that
the CLI exits and Docker removes the trial container.

Set `BRIDGE_CREDENTIAL_DIR` to a private host directory for the scripted
sentinel probe. The default is `~/.codex-hub-client`.

## Run the approved subscription check

Get approval for one product control and one Harbor trial. Each execution has
a ten-minute deadline and one attempt. Use the same HTTPS CLI Proxy Hub route,
CA and dedicated `factory-delivery` credential as Phase 03.

```sh
BRIDGE_MODE=live \
BRIDGE_APPROVED_BY="$MAINTAINER" \
BRIDGE_HUB_URL="$HUB_URL" \
BRIDGE_HUB_KEY_FILE="$HUB_KEY_FILE" \
BRIDGE_HUB_CA_FILE="$HUB_CA_FILE" \
make verify-harbor-bridge
```

The command checks the Hub route against the archived Phase 03 request and
places the harmless sentinel beside the key for the workspace isolation probe.
Both executions resolve the same bot at the tested commit and run the same
Codex adapter. The Hub credential is read on the trusted host. Trial containers
have offline networking and receive workspace operations through their own
revocable bindings.

## Read the evidence

The command prints the private evidence directory. Start with `summary.json`.
It includes the tested commit, workflow and CLI builds, model settings, skill
digests, locked task and grader, rollout and Temporal IDs, artifact digests,
reported usage, protected grade, fixture receipt, and provider cleanup.

Open the partial manifest under `runs/eval-<rollout>/harbor-export.json` to find
the bundle, binary-capable patch, Codex events, ATIF trajectory and usage in the
content-addressed `blobs/` directory. Harbor retains its queue, lock and
protected results under `harbor/`. The supervisor reads the persisted grade
after the agent container stops. The verification suppresses the Temporal grade
signal and reopens the resulting receipt twice to check deduplication.
