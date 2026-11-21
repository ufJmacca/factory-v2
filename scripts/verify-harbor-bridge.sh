#!/bin/sh
set -eu
umask 077

mode=${BRIDGE_MODE:-scripted}
case "$mode" in
    scripted) ;;
    live)
        : "${BRIDGE_APPROVED_BY:?Set the name of the maintainer who approved two single-attempt, ten-minute executions}"
        : "${BRIDGE_HUB_URL:?Set the approved Phase 03 HTTPS Hub endpoint}"
        : "${BRIDGE_HUB_KEY_FILE:?Set the existing dedicated factory credential file}"
        : "${BRIDGE_HUB_CA_FILE:?Set the trusted Hub CA file}"
        ;;
    *) printf 'BRIDGE_MODE must be scripted or live\n' >&2; exit 2 ;;
esac
input_root=${HARBOR_INPUT_ROOT:-"$HOME/.local/state/factory-v2/harbor-v1"}
HARBOR_P03_ARCHIVE=$(cd "${HARBOR_P03_ARCHIVE:-"$input_root/p03"}" && pwd -P)
HARBOR_RECORDS_ARCHIVE=$(cd "${HARBOR_RECORDS_ARCHIVE:-"$input_root/records"}" && pwd -P)
HARBOR_HELD_OUT_DIR=$(cd "${HARBOR_HELD_OUT_DIR:-"$input_root/held-out"}" && pwd -P)

mkdir -p "${EVIDENCE_ROOT:-.evidence}"
evidence_root=$(cd "${EVIDENCE_ROOT:-.evidence}" && pwd -P)
if [ -n "${BRIDGE_RESUME_ROOT:-}" ]; then
    BRIDGE_ROOT=$(cd "$BRIDGE_RESUME_ROOT" && pwd -P)
    git diff --exit-code "$(cat "$BRIDGE_ROOT/factory-sha.txt")" HEAD -- src grading agents.toml .agents/skills \
        evaluation/harbor/factory_agent.py compose.harbor-bridge.yaml
else
    BRIDGE_ROOT=$(mktemp -d "$evidence_root/verify-harbor-bridge.XXXXXX")
    git rev-parse HEAD > "$BRIDGE_ROOT/factory-sha.txt"
fi
BRIDGE_WATCH_STOP=$(mktemp -d "$BRIDGE_ROOT/watcher.XXXXXX")/stop
HARBOR_RUN_DIR=$BRIDGE_ROOT
OUTCOME_EVIDENCE_ROOT=$BRIDGE_ROOT
BRIDGE_API_KEY=$(openssl rand -hex 32)
export BRIDGE_ROOT HARBOR_RUN_DIR OUTCOME_EVIDENCE_ROOT BRIDGE_API_KEY BRIDGE_WATCH_STOP
export HARBOR_P03_ARCHIVE HARBOR_RECORDS_ARCHIVE HARBOR_HELD_OUT_DIR
printf 'Harbor bridge evidence %s\n' "$BRIDGE_ROOT"
git status --porcelain > "$BRIDGE_ROOT/tested-status.txt"
git rev-parse HEAD > "$BRIDGE_ROOT/tested-sha.txt"
if [ "$mode" = live ] && [ -s "$BRIDGE_ROOT/tested-status.txt" ]; then
    printf 'Run the approved live verification from a clean checkout.\n' >&2
    exit 2
fi
project="factory-bridge-$(date +%s)-$$"

compose() {
    docker compose -p "$project" -f compose.yaml -f compose.recovery.yaml -f compose.codex.yaml \
        -f compose.harbor.yaml -f compose.harbor-bridge.yaml --profile harbor "$@"
}

runner() {
    PYTHONPATH=src:. "${FACTORY_HOST_PYTHON:-python3.12}" -m scripts.probe_harbor_runner "$@"
}

sentinel=
watcher=
cleanup() {
    touch "$BRIDGE_WATCH_STOP"
    if [ -n "$watcher" ]; then wait "$watcher" || :; fi
    compose down --volumes --timeout 15 > "$BRIDGE_ROOT/cleanup.log" 2>&1 || :
    if [ -n "$sentinel" ]; then rm -f "$sentinel"; fi
}
trap cleanup EXIT

compose build harbor codex-workspace outcome-worker > "$BRIDGE_ROOT/build.log" 2>&1
if [ -z "${BRIDGE_RESUME_ROOT:-}" ]; then
    compose run --rm --no-deps -T -e BRIDGE_ROOT harbor \
        uv run --project /opt/harbor --python /usr/local/bin/python --locked --offline --no-sync \
        python -m evaluation.harbor.bridge_stage > "$BRIDGE_ROOT/stage.log" 2>&1
fi
image=$(docker image inspect "$project-codex-workspace" --format '{{.Id}}')
worker=$(docker image inspect "$project-outcome-worker" --format '{{.Id}}')
if [ -n "${BRIDGE_RESUME_ROOT:-}" ]; then
    worker=$("${FACTORY_HOST_PYTHON:-python3.12}" -c \
        'import json, sys; print(json.load(open(sys.argv[1]))["runtime"]["worker_image"])' \
        "$BRIDGE_ROOT/runs/bridge/common.json")
    docker tag "$worker" "$project-outcome-worker"
fi
runner prepare "$BRIDGE_ROOT" "$(cat "$BRIDGE_ROOT/factory-sha.txt")" "$image" "$worker" "$mode"
credential_dir=${BRIDGE_CREDENTIAL_DIR:-"$HOME/.codex-hub-client"}
if [ "$mode" = live ]; then credential_dir=$(dirname "$BRIDGE_HUB_KEY_FILE"); fi
sentinel=$(mktemp "$credential_dir/factory-bridge-sentinel.XXXXXX")
printf 'factory bridge probe\n' > "$sentinel"
compose up --detach --no-build --wait --wait-timeout 45 harbor-bridge outcome-worker harbor-outcome-worker > "$BRIDGE_ROOT/compose.log" 2>&1
server=$(compose ps -q harbor-bridge)
runner watch "$BRIDGE_ROOT" "${FACTORY_CODEX_CLI:-codex}" "$sentinel" \
    "$HARBOR_RECORDS_ARCHIVE" "$server" > "$BRIDGE_ROOT/runner.log" 2>&1 &
watcher=$!
if ! compose exec -T -e BRIDGE_MODE="$mode" -e BRIDGE_RESUME_ROOT="${BRIDGE_RESUME_ROOT:-}" outcome-worker python -m evaluation.harbor.verify_bridge \
    > "$BRIDGE_ROOT/supervisor.log" 2>&1; then
    cat "$BRIDGE_ROOT/supervisor.log" >&2
    compose logs harbor-bridge outcome-worker harbor-outcome-worker > "$BRIDGE_ROOT/service.log" 2>&1
    exit 1
fi
printf 'Harbor bridge summary %s/summary.json\n' "$BRIDGE_ROOT"
