#!/bin/sh
set -eu
umask 077

mode=${PHASE08_MODE:-scripted}
case "$mode" in
    scripted) ;;
    live)
        : "${PHASE08_APPROVED_BY:?Set the maintainer who approved the 18-execution Phase 08 envelope}"
        : "${BRIDGE_HUB_URL:?Set the original Phase 03 HTTPS Hub endpoint}"
        : "${BRIDGE_HUB_KEY_FILE:?Set the dedicated factory-delivery key file}"
        : "${BRIDGE_HUB_CA_FILE:?Set the trusted Hub CA file}"
        ;;
    *) printf 'PHASE08_MODE must be scripted or live\n' >&2; exit 2 ;;
esac

input_root=${HARBOR_INPUT_ROOT:-"$HOME/.local/state/factory-v2/harbor-v1"}
HARBOR_P03_ARCHIVE=$(cd "${HARBOR_P03_ARCHIVE:-"$input_root/p03"}" && pwd -P)
HARBOR_RECORDS_ARCHIVE=$(cd "${HARBOR_RECORDS_ARCHIVE:-"$input_root/records"}" && pwd -P)
HARBOR_HELD_OUT_DIR=$(cd "${HARBOR_HELD_OUT_DIR:-"$input_root/held-out"}" && pwd -P)
controls=$(cd "${PHASE08_CONTROLS:-"$input_root/phase08-controls"}" && pwd -P)
baseline=${PHASE08_BASELINE:-ac353c86ae328259f862aba40b3efc4edd82a69a}
build=$(git rev-parse HEAD)
if [ -n "${PHASE08_RESUME:-}" ]; then
    BRIDGE_ROOT=$(cd "$PHASE08_RESUME" && pwd -P)
    test "$(cat "$BRIDGE_ROOT/tested-sha.txt")" = "$build"
    test "$(cat "$BRIDGE_ROOT/mode.txt")" = "$mode"
else
    mkdir -p "${EVIDENCE_ROOT:-.evidence}"
    root=$(cd "${EVIDENCE_ROOT:-.evidence}" && pwd -P)
    BRIDGE_ROOT=$(mktemp -d "$root/verify-harbor-experiment.XXXXXX")
    openssl rand -hex 32 > "$BRIDGE_ROOT/server-token"
    printf '%s\n' "$build" > "$BRIDGE_ROOT/tested-sha.txt"
    printf '%s\n' "$mode" > "$BRIDGE_ROOT/mode.txt"
    git status --porcelain > "$BRIDGE_ROOT/tested-status.txt"
    cp -R "$controls" "$BRIDGE_ROOT/controls"
fi
if [ "$mode" = live ] && [ -n "$(git status --porcelain)" ]; then
    printf 'Run live comparison from a clean checkout.\n' >&2
    exit 2
fi
printf 'Phase 08 trusted evidence %s\n' "$BRIDGE_ROOT"
project="factory-p08-$(basename "$BRIDGE_ROOT" | tr '[:upper:]' '[:lower:]' | tr '.' '-')"
HARBOR_RUN_DIR=$BRIDGE_ROOT
OUTCOME_EVIDENCE_ROOT=$BRIDGE_ROOT
BRIDGE_API_KEY=$(cat "$BRIDGE_ROOT/server-token")
BRIDGE_WATCH_STOP=$(mktemp -d "$BRIDGE_ROOT/watch.XXXXXX")/stop
export BRIDGE_ROOT HARBOR_RUN_DIR OUTCOME_EVIDENCE_ROOT BRIDGE_API_KEY BRIDGE_WATCH_STOP
export HARBOR_P03_ARCHIVE HARBOR_RECORDS_ARCHIVE HARBOR_HELD_OUT_DIR

compose() {
    docker compose -p "$project" -f compose.yaml -f compose.recovery.yaml -f compose.codex.yaml \
        -f compose.harbor.yaml -f compose.harbor-bridge.yaml --profile harbor "$@"
}

runner() {
    PYTHONPATH=src:. "${FACTORY_HOST_PYTHON:-python3.12}" -m "$@"
}

controller() {
    compose exec -T -e PHASE08_APPROVED_BY="${PHASE08_APPROVED_BY:-}" outcome-worker \
        python -m evaluation.harbor.verify_experiment "$1" "$mode"
}

watcher=
sentinel=
cleanup() {
    touch "$BRIDGE_WATCH_STOP"
    if [ -n "$watcher" ]; then wait "$watcher" || :; fi
    if [ -n "$sentinel" ]; then rm -f "$sentinel"; fi
    if [ -f "$BRIDGE_ROOT/summary.json" ] || [ ! -f "$BRIDGE_ROOT/experiment.json" ]; then
        compose down --volumes --timeout 15 > "$BRIDGE_ROOT/cleanup.log" 2>&1 || :
    else
        printf 'Retained project for reconciliation. Resume with PHASE08_RESUME=%s.\n' "$BRIDGE_ROOT"
    fi
}
trap cleanup EXIT
trap 'touch "$BRIDGE_ROOT/stop"; exit 130' INT TERM

if [ ! -f "$BRIDGE_ROOT/experiment.json" ]; then
    compose build harbor outcome-worker > "$BRIDGE_ROOT/build.log" 2>&1
    compose run --rm --no-deps -T -e BRIDGE_ROOT harbor \
        uv run --project /opt/harbor --python /usr/local/bin/python --locked --offline --no-sync \
        python -m evaluation.harbor.experiment_stage > "$BRIDGE_ROOT/stage.log" 2>&1
    worker=$(docker image inspect "$project-outcome-worker" --format '{{.Id}}')
    docker image inspect "$project-harbor" --format '{{.Id}}' > "$BRIDGE_ROOT/harbor-image.txt"
    runner evaluation.harbor.experiment_inputs \
        "$BRIDGE_ROOT" "$HARBOR_P03_ARCHIVE" "$baseline" "$build" "$worker" "$mode"
fi
worker=$(jq -r '.arms.baseline.runtime.worker_image' "$BRIDGE_ROOT/runs/experiment/configuration.json")
test "$(docker image inspect "$project-outcome-worker" --format '{{.Id}}')" = "$worker"
test "$(docker image inspect "$project-harbor" --format '{{.Id}}')" = "$(cat "$BRIDGE_ROOT/harbor-image.txt")"
credential_dir=${BRIDGE_CREDENTIAL_DIR:-"$HOME/.codex-hub-client"}
if [ "$mode" = live ]; then credential_dir=$(dirname "$BRIDGE_HUB_KEY_FILE"); fi
sentinel=$(mktemp "$credential_dir/factory-phase08-sentinel.XXXXXX")
printf 'factory bridge probe\n' > "$sentinel"
compose up --detach --no-build --wait --wait-timeout 45 harbor-bridge outcome-worker harbor-outcome-worker \
    > "$BRIDGE_ROOT/compose.log" 2>&1
for service in outcome-worker harbor-outcome-worker; do
    test "$(docker inspect "$(compose ps -q "$service")" --format '{{.Image}}')" = "$worker"
done
server=$(compose ps -q harbor-bridge)
test "$(docker inspect "$server" --format '{{.Image}}')" = "$(cat "$BRIDGE_ROOT/harbor-image.txt")"
runner scripts.probe_harbor_runner watch "$BRIDGE_ROOT" "${FACTORY_CODEX_CLI:-codex}" "$sentinel" \
    "$HARBOR_RECORDS_ARCHIVE" "$server" >> "$BRIDGE_ROOT/runner.log" 2>&1 &
watcher=$!
if [ ! -f "$BRIDGE_ROOT/experiment.json" ]; then
    controller freeze >> "$BRIDGE_ROOT/supervisor.log" 2>&1
    if [ "$mode" = scripted ]; then controller lose-reply >> "$BRIDGE_ROOT/supervisor.log" 2>&1; fi
fi
if ! controller run >> "$BRIDGE_ROOT/supervisor.log" 2>&1; then
    tail -30 "$BRIDGE_ROOT/supervisor.log" >&2
    exit 1
fi
printf 'Phase 08 result %s/summary.json\n' "$BRIDGE_ROOT"
