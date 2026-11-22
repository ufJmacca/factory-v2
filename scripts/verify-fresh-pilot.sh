#!/bin/sh
set -eu
umask 077

mode=${FRESH_PILOT_MODE:-scripted}
action=${FRESH_PILOT_ACTION:-run}
scope=${FRESH_PILOT_SCOPE:-historical}
case "$mode:$action:$scope" in
    scripted:freeze:historical|scripted:run:historical|scripted:freeze:full|scripted:run:full|live:freeze:full|live:run:full) ;;
    *) printf 'Invalid fresh pilot mode, action, or scope. Live mode requires full scope.\n' >&2; exit 2 ;;
esac
if [ "$mode:$action" = live:run ]; then
    : "${FRESH_PILOT_RESUME:?Freeze the full live execution manifest first}"
    : "${FRESH_PILOT_APPROVAL:?Provide the separately approved execution record}"
fi
build=$(git rev-parse HEAD)
status=$(git status --porcelain)
if [ -n "$status" ] && { [ "$mode" = live ] || [ "${FRESH_PILOT_ALLOW_DIRTY:-0}" != 1 ]; }; then
    printf 'Run the fresh pilot from a clean checkout.\n' >&2
    exit 2
fi

input_root=${HARBOR_INPUT_ROOT:-"$HOME/.local/state/factory-v2/harbor-v1"}
HARBOR_P03_ARCHIVE=$(cd "${HARBOR_P03_ARCHIVE:-"$input_root/p03"}" && pwd -P)
HARBOR_RECORDS_ARCHIVE=$(cd "${HARBOR_RECORDS_ARCHIVE:-"$input_root/records"}" && pwd -P)
HARBOR_HELD_OUT_DIR=$(cd "${HARBOR_HELD_OUT_DIR:-"$input_root/held-out"}" && pwd -P)
checkout=$(pwd -P)
if [ -n "${FRESH_PILOT_RESUME:-}" ]; then
    BRIDGE_ROOT=$(cd "$FRESH_PILOT_RESUME" && pwd -P)
    test -f "$BRIDGE_ROOT/experiment.json"
    test "$(cat "$BRIDGE_ROOT/tested-sha.txt")" = "$build"
    test "$(cat "$BRIDGE_ROOT/tested-status.txt")" = "$status"
    test "$(cat "$BRIDGE_ROOT/mode.txt")" = "$mode"
    test "$(cat "$BRIDGE_ROOT/scope.txt")" = "$scope"
else
    : "${FRESH_PILOT_DESIGN:?Provide the approved design file}"
    : "${FRESH_PILOT_DESIGN_SHA256:?Provide the approved design digest}"
    mkdir -p "${EVIDENCE_ROOT:-.evidence}"
    root=$(cd "${EVIDENCE_ROOT:-.evidence}" && pwd -P)
    BRIDGE_ROOT=$(mktemp -d "$root/fresh-pilot.XXXXXX")
    install -m 0600 "$FRESH_PILOT_DESIGN" "$BRIDGE_ROOT/design-input.json"
    test "$(shasum -a 256 < "$BRIDGE_ROOT/design-input.json" | cut -d ' ' -f 1)" = "$FRESH_PILOT_DESIGN_SHA256"
    openssl rand -hex 32 > "$BRIDGE_ROOT/server-token"
    printf '%s\n' "$build" > "$BRIDGE_ROOT/tested-sha.txt"
    printf '%s\n' "$status" > "$BRIDGE_ROOT/tested-status.txt"
    printf '%s\n' "$mode" > "$BRIDGE_ROOT/mode.txt"
    printf '%s\n' "$scope" > "$BRIDGE_ROOT/scope.txt"
fi

printf 'Phase 09 trusted evidence %s\n' "$BRIDGE_ROOT"
project="factory-p09-$(basename "$BRIDGE_ROOT" | tr '[:upper:].' '[:lower:]-')"
HARBOR_RUN_DIR=$BRIDGE_ROOT
OUTCOME_EVIDENCE_ROOT=$BRIDGE_ROOT
BRIDGE_API_KEY=$(cat "$BRIDGE_ROOT/server-token")
export BRIDGE_ROOT HARBOR_RUN_DIR OUTCOME_EVIDENCE_ROOT BRIDGE_API_KEY
export HARBOR_P03_ARCHIVE HARBOR_RECORDS_ARCHIVE HARBOR_HELD_OUT_DIR

compose() {
    docker compose -p "$project" -f compose.yaml -f compose.recovery.yaml -f compose.codex.yaml \
        -f compose.harbor.yaml -f compose.harbor-bridge.yaml --profile harbor "$@"
}

offline_worker() {
    docker run --rm --pull never --network none --user "$(id -u):$(id -g)" \
        -v "$BRIDGE_ROOT:$BRIDGE_ROOT" -v "$checkout:/checkout:ro" -v "$HARBOR_P03_ARCHIVE:/p03:ro" \
        -e BRIDGE_ROOT -e PYTHONPATH=/factory/src:/checkout -e HOME=/tmp -w /checkout "$@"
}

stage() {
    set -- run --rm --no-deps --pull never -T -e BRIDGE_ROOT
    if [ -n "$protected" ]; then
        set -- "$@" -v "$protected:/pilot-protected:ro"
    fi
    set -- "$@" harbor \
        uv run --project /opt/harbor --python /usr/local/bin/python --locked --offline --no-sync \
        python -m evaluation.harbor.pilot_inputs stage "$BRIDGE_ROOT/design-input.json" "$FRESH_PILOT_DESIGN_SHA256"
    if [ -n "$protected" ]; then set -- "$@" --protected "$protected"; fi
    compose "$@"
}

runner() {
    PYTHONPATH=src:. "${FACTORY_HOST_PYTHON:-python3.12}" -m scripts.probe_harbor_runner "$@"
}

watcher=
sentinel=
started=false
cleanup() {
    if [ -n "$watcher" ]; then touch "$BRIDGE_WATCH_STOP"; wait "$watcher" || :; fi
    if [ -n "$sentinel" ]; then rm -f "$sentinel"; fi
    if [ "$started" = true ]; then
        if [ -f "$BRIDGE_ROOT/summary.json" ]; then
            compose down --volumes --timeout 15 > "$BRIDGE_ROOT/cleanup.log" 2>&1 || :
        else
            printf 'Retained project for reconciliation. Resume with FRESH_PILOT_RESUME=%s.\n' "$BRIDGE_ROOT"
        fi
    fi
}
trap cleanup EXIT
trap 'touch "$BRIDGE_ROOT/stop"; exit 130' INT TERM

cli=${FACTORY_CODEX_CLI:-codex}
mkdir -p "$BRIDGE_ROOT/cli-version"
cli_build=$(CODEX_HOME="$BRIDGE_ROOT/cli-version" "$cli" --version)
if [ ! -f "$BRIDGE_ROOT/experiment.json" ]; then
    harbor_image=$(jq -er '.runtime.harbor_image' "$BRIDGE_ROOT/design-input.json")
    test "$(docker image inspect "$harbor_image" --format '{{.Id}}')" = "$harbor_image"
    docker image tag "$harbor_image" "$project-harbor"
    printf '%s\n' "$harbor_image" > "$BRIDGE_ROOT/harbor-image.txt"
    compose build outcome-worker > "$BRIDGE_ROOT/build.log" 2>&1
    protected=
    if [ "$scope" = full ]; then
        : "${FRESH_PILOT_PROTECTED:?Provide the private calibrated task locks}"
        protected=$(cd "$FRESH_PILOT_PROTECTED" && pwd -P)
    fi
    stage > "$BRIDGE_ROOT/stage.log" 2>&1
    worker=$(docker image inspect "$project-outcome-worker" --format '{{.Id}}')
    offline_worker "$worker" uv run --project /workspace --locked --offline --no-sync \
        python -m evaluation.harbor.pilot_inputs configure "$build" "$worker" "$mode" "$cli_build" /checkout \
        > "$BRIDGE_ROOT/configuration.log" 2>&1
    offline_worker "$worker" uv run --project /workspace --locked --offline --no-sync \
        python -m evaluation.harbor.verify_experiment freeze "$mode" fresh-pilot > "$BRIDGE_ROOT/supervisor.log" 2>&1
fi
if [ "$action" = freeze ]; then
    printf 'Phase 09 execution manifest %s/experiment.json\n' "$BRIDGE_ROOT"
    exit 0
fi

worker=$(jq -er '.arms.baseline.runtime.worker_image' "$BRIDGE_ROOT/runs/experiment/configuration.json")
test "$cli_build" = "$(jq -er '.arms.baseline.runtime.cli_build' "$BRIDGE_ROOT/runs/experiment/configuration.json")"
test "$(docker image inspect "$project-outcome-worker" --format '{{.Id}}')" = "$worker"
test "$(docker image inspect "$project-harbor" --format '{{.Id}}')" = "$(cat "$BRIDGE_ROOT/harbor-image.txt")"
if [ "$mode" = live ]; then
    approval=$(cd "$(dirname "$FRESH_PILOT_APPROVAL")" && printf '%s/%s\n' "$(pwd -P)" "$(basename "$FRESH_PILOT_APPROVAL")")
    offline_worker -v "$approval:/pilot-approval.json:ro" "$worker" \
        uv run --project /workspace --locked --offline --no-sync \
        python -m evaluation.harbor.pilot_inputs approve /pilot-approval.json
    : "${BRIDGE_HUB_URL:?Set the original Phase 03 HTTPS Hub endpoint}"
    : "${BRIDGE_HUB_KEY_FILE:?Set the dedicated factory-delivery key file}"
    : "${BRIDGE_HUB_CA_FILE:?Set the trusted Hub CA file}"
    runner check-live-transport "$BRIDGE_ROOT"
    sentinel=$(mktemp "$(dirname "$BRIDGE_HUB_KEY_FILE")/factory-phase09-sentinel.XXXXXX")
else
    FACTORY_HARBOR_SCRIPTED_ONLY=1
    export FACTORY_HARBOR_SCRIPTED_ONLY
    sentinel=$(mktemp "$BRIDGE_ROOT/scripted-sentinel.XXXXXX")
fi
printf 'factory bridge probe\n' > "$sentinel"
BRIDGE_WATCH_STOP=$(mktemp -d "$BRIDGE_ROOT/watch.XXXXXX")/stop
export BRIDGE_WATCH_STOP
started=true
compose up --detach --no-build --pull never --wait --wait-timeout 45 harbor-bridge outcome-worker harbor-outcome-worker \
    > "$BRIDGE_ROOT/compose.log" 2>&1
for service in outcome-worker harbor-outcome-worker; do
    test "$(docker inspect "$(compose ps -q "$service")" --format '{{.Image}}')" = "$worker"
done
server=$(compose ps -q harbor-bridge)
test "$(docker inspect "$server" --format '{{.Image}}')" = "$(cat "$BRIDGE_ROOT/harbor-image.txt")"
compose exec -T outcome-worker python - <<'PY' >> "$BRIDGE_ROOT/compose.log" 2>&1
import os
from time import monotonic, sleep
from urllib.error import URLError

from factory.harbor_supervisor import Harbor

api = Harbor(os.environ["BRIDGE_API_KEY"])
deadline = monotonic() + 45
while monotonic() < deadline:
    try:
        status, _ = api.exchange("/jobs")
        if status != 200:
            raise SystemExit(f"Harbor readiness returned HTTP {status}")
        break
    except URLError:
        sleep(0.25)
else:
    raise SystemExit("Harbor API did not start")
PY
runner watch "$BRIDGE_ROOT" "$cli" "$sentinel" "$HARBOR_RECORDS_ARCHIVE" "$server" >> "$BRIDGE_ROOT/runner.log" 2>&1 &
watcher=$!
if ! compose exec -T outcome-worker python -m evaluation.harbor.verify_experiment run "$mode" fresh-pilot \
    >> "$BRIDGE_ROOT/supervisor.log" 2>&1; then
    tail -30 "$BRIDGE_ROOT/supervisor.log" >&2
    exit 1
fi
printf 'Phase 09 result %s/summary.json\n' "$BRIDGE_ROOT"
