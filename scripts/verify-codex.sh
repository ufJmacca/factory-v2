#!/bin/sh
set -eu
umask 077

mkdir -p "${EVIDENCE_ROOT:-.evidence}"
evidence_root=$(cd "${EVIDENCE_ROOT:-.evidence}" && pwd -P)
evidence=$(mktemp -d "$evidence_root/verify-codex.XXXXXX")
printf 'Evidence directory %s\n' "$evidence"
git status --porcelain > "$evidence/git-status.txt"
git rev-parse HEAD > "$evidence/factory-sha.txt"
project="factory-codex-check-$(date +%s)-$$"
export OUTCOME_EVIDENCE_ROOT="$evidence"

compose() {
    docker compose -p "$project" -f compose.yaml -f compose.recovery.yaml -f compose.codex.yaml "$@"
}

if ! compose build workspace codex-workspace outcome-worker > "$evidence/build.log" 2>&1; then
    cat "$evidence/build.log" >&2
    exit 1
fi
if ! compose run --rm --no-deps -T workspace uv run --locked --offline \
    python -m unittest discover -s tests -v > "$evidence/tests.log" 2>&1; then
    cat "$evidence/tests.log" >&2
    exit 1
fi
cat "$evidence/tests.log"
if ! PYTHONPATH=src "${FACTORY_HOST_PYTHON:-python3.12}" scripts/probe_codex_workspace.py \
    "$project-codex-workspace" "$project" "$evidence" > "$evidence/workspace.log" 2>&1; then
    cat "$evidence/workspace.log" >&2
    exit 1
fi
cat "$evidence/workspace.log"
if ! PYTHONPATH=src "${FACTORY_HOST_PYTHON:-python3.12}" scripts/probe_codex_cli.py \
    "$project-codex-workspace" "$project" "$evidence" "${FACTORY_CODEX_CLI:-codex}" \
    > "$evidence/cli.log" 2>&1; then
    cat "$evidence/cli.log" >&2
    exit 1
fi
cat "$evidence/cli.log"

trap 'compose down --volumes --timeout 5 >> "$evidence/temporal-compose.log" 2>&1 || :' EXIT
if ! compose up -d --no-build --wait --wait-timeout 30 outcome-worker \
    > "$evidence/temporal-compose.log" 2>&1; then
    cat "$evidence/temporal-compose.log" >&2
    exit 1
fi
if ! compose exec -T outcome-worker python - < scripts/probe_codex_deadline.py \
    > "$evidence/temporal.log" 2>&1; then
    cat "$evidence/temporal.log" >&2
    exit 1
fi
cat "$evidence/temporal.log"
