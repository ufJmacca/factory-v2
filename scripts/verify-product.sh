#!/bin/sh
set -eu
umask 077

mkdir -p "${EVIDENCE_ROOT:-.evidence}"
root=$(cd "${EVIDENCE_ROOT:-.evidence}" && pwd -P)
evidence=$(mktemp -d "$root/verify-product.XXXXXX")
printf 'Evidence directory %s\n' "$evidence"
git status --porcelain > "$evidence/git-status.txt"
git rev-parse HEAD > "$evidence/factory-sha.txt"
project="factory-product-check-$(date +%s)-$$"
export OUTCOME_EVIDENCE_ROOT="$evidence"

compose() {
    docker compose -p "$project" -f compose.yaml -f compose.recovery.yaml -f compose.codex.yaml "$@"
}

run_logged() {
    log=$1
    shift
    if ! "$@" > "$evidence/$log.log" 2>&1; then
        cat "$evidence/$log.log" >&2
        exit 1
    fi
    cat "$evidence/$log.log"
}

run_logged build compose build workspace outcome-worker
run_logged tests compose run --rm --no-deps --pull never -T workspace uv run --locked --offline \
    python -m unittest discover -s tests -p 'test_product*.py' -v

trap 'compose down --volumes --timeout 5 >> "$evidence/compose.log" 2>&1 || :' EXIT
run_logged compose compose up -d --no-build --wait --wait-timeout 30 outcome-worker
compose cp tests/test_product_delivery.py outcome-worker:/tmp/product_probe_support.py
run_logged temporal compose exec -T outcome-worker python - < scripts/probe_product_delivery.py
compose down --volumes --timeout 5 >> "$evidence/compose.log" 2>&1
trap - EXIT

run_logged reopen compose run --rm --no-deps --pull never -T -v "$evidence:/evidence:ro" \
    -e PYTHONPATH=src workspace uv run --locked --offline python -c '
from pathlib import Path
from factory.evidence import Evidence
from factory.product import reopen_product

store = Evidence(Path("/evidence"))
for run_id, expected in (("product-lost-reply", "observed"), ("product-withheld", "unshipped")):
    manifest = reopen_product(store, run_id)
    assert manifest["status"] == expected, manifest
    print(run_id, manifest["status"], manifest["accepted_revision"], manifest["execution"]["workflow_id"])
assert store.read("product-lost-reply", "provider-write-counts") == {"push": 1, "pull_request": 1}
print("Reopened the archive after removing the worker and its Temporal volume.")
'
printf 'model_calls=0\nexternal_service_writes=0\n' > "$evidence/run.txt"
