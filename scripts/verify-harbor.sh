#!/bin/sh
set -eu
umask 077

input_root=${HARBOR_INPUT_ROOT:-"$HOME/.local/state/factory-v2/harbor-v1"}

trusted_directory() {
    if [ "${1#/}" = "$1" ] || [ ! -d "$1" ]; then
        printf 'Set %s to the absolute path of the trusted input directory.\n' "$2" >&2
        return 2
    fi
    (cd "$1" && pwd -P)
}

HARBOR_P03_ARCHIVE=$(trusted_directory "${HARBOR_P03_ARCHIVE:-"$input_root/p03"}" HARBOR_P03_ARCHIVE)
HARBOR_RECORDS_ARCHIVE=$(trusted_directory "${HARBOR_RECORDS_ARCHIVE:-"$input_root/records"}" HARBOR_RECORDS_ARCHIVE)
HARBOR_HELD_OUT_DIR=$(trusted_directory "${HARBOR_HELD_OUT_DIR:-"$input_root/held-out"}" HARBOR_HELD_OUT_DIR)

mkdir -p "${EVIDENCE_ROOT:-.evidence}"
evidence_root=$(cd "${EVIDENCE_ROOT:-.evidence}" && pwd -P)
HARBOR_RUN_DIR=$(mktemp -d "$evidence_root/verify-harbor.XXXXXX")
HARBOR_FACTORY_SHA=$(git rev-parse HEAD)
HARBOR_FACTORY_CLEAN=false
if [ -z "$(git status --porcelain)" ]; then HARBOR_FACTORY_CLEAN=true; fi
export HARBOR_RUN_DIR HARBOR_FACTORY_SHA HARBOR_FACTORY_CLEAN
export HARBOR_P03_ARCHIVE HARBOR_RECORDS_ARCHIVE HARBOR_HELD_OUT_DIR
printf 'Harbor evidence directory %s\n' "$HARBOR_RUN_DIR"

docker compose -f compose.yaml -f compose.harbor.yaml --profile harbor build harbor
docker compose -f compose.yaml -f compose.harbor.yaml --profile harbor run --rm --no-deps -T \
    -e HARBOR_RUN_DIR -e HARBOR_FACTORY_SHA -e HARBOR_FACTORY_CLEAN harbor \
    uv run --project /opt/harbor --python /usr/local/bin/python --locked --offline --no-sync \
    python -m evaluation.harbor.calibrate
printf 'Harbor summary %s/summary.json\n' "$HARBOR_RUN_DIR"
