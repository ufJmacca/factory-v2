#!/bin/sh
set -eu
umask 077

project="factory-recovery-$(date +%s)-$$"
crash="$project-crash"
fresh="$project-fresh"
mkdir -p "${EVIDENCE_ROOT:-.evidence}"
evidence_root=$(cd "${EVIDENCE_ROOT:-.evidence}" && pwd -P)
evidence="$evidence_root/$project"
mkdir "$evidence"
printf 'Evidence directory %s\n' "$evidence"

compose() {
    docker compose -p "$project" -f compose.yaml -f compose.recovery.yaml "$@"
}

control() {
    compose exec -T control python -m factory.recovery_cli "$@"
}

containers() {
    compose ps -aq >> "$evidence/containers.txt"
}

cleanup() {
    result=$?
    trap - EXIT
    compose ps -a > "$evidence/compose-ps.txt" 2>&1 || :
    compose logs --no-color > "$evidence/compose.log" 2>&1 || :
    compose down --remove-orphans --timeout 5 >> "$evidence/compose.log" 2>&1 || :
    if [ "$result" -ne 0 ]; then
        printf 'Recovery drill failed. Logs in %s\n' "$evidence" >&2
        tail -30 "$evidence/compose.log" >&2
    fi
    exit "$result"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

git status --porcelain > "$evidence/git-status.txt"
git rev-parse HEAD > "$evidence/factory-sha.txt"
if ! compose build workspace worker candidate recovery-verifier > "$evidence/build.log" 2>&1; then
    cat "$evidence/build.log" >&2
    exit 1
fi
if ! compose pull temporal >> "$evidence/build.log" 2>&1; then
    cat "$evidence/build.log" >&2
    exit 1
fi
image_id() {
    docker image inspect --format '{{.Id}}' "$1"
}
{
    worker_image=$(image_id "$project-delivery-worker")
    candidate_image=$(image_id "$project-recovery-service")
    verifier_image=$(image_id "$project-recovery-verifier")
    temporal_image=$(image_id "$(compose config --images temporal)")
    printf 'worker %s\ncandidate_and_fixture %s\nverifier %s\ntemporal %s\n' \
        "$worker_image" "$candidate_image" "$verifier_image" "$temporal_image"
} > "$evidence/environment.txt"
if command -v sha256sum >/dev/null 2>&1; then
    digest_line=$(sha256sum "$evidence/environment.txt")
else
    digest_line=$(shasum -a 256 "$evidence/environment.txt")
fi
export RECOVERY_ENVIRONMENT_DIGEST="${digest_line%% *}"
export RECOVERY_CRASH_RUN="$crash"
export COMPOSE_IGNORE_ORPHANS=true

started=$(date +%s)
if ! compose run --no-deps --pull never -T workspace \
    uv run --locked --offline python -m unittest discover -s tests -v > "$evidence/test.log" 2>&1; then
    cat "$evidence/test.log" >&2
    exit 1
fi
cat "$evidence/test.log"

compose up -d --no-build --wait --wait-timeout 45 worker control
containers
worker=$(compose ps -q worker)
candidate=$(compose ps -q candidate)
temporal=$(compose ps -q temporal)

control submit "$crash" > "$evidence/crash-start.json"
control submit "$crash" > "$evidence/crash-duplicate-running.json"
if control submit "$crash" --base 0000000000000000000000000000000000000000 \
    > "$evidence/crash-conflict.log" 2>&1; then
    printf 'Conflicting request was accepted\n' >&2
    exit 1
fi

deadline=$(($(date +%s) + 60))
while [ "$(docker inspect --format '{{.State.Running}}' "$worker")" = true ]; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        printf 'Timed out waiting for the worker failpoint\n' >&2
        exit 1
    fi
    sleep 1
done
docker inspect --format '{{json .State}}' "$worker" > "$evidence/killed-worker.json"
test "$(docker inspect --format '{{.State.ExitCode}}' "$worker")" = 137
control status "$crash" > "$evidence/crash-pending.json"
control fixture > "$evidence/fixture-before-restart.json"
compose cp control:/evidence "$evidence/before"
compose exec -T candidate test -d "/tmp/checkouts/$crash/.git"

compose rm -sf worker candidate
unset RECOVERY_CRASH_RUN
compose up -d --no-build --wait --wait-timeout 25 candidate
new_candidate=$(compose ps -q candidate)
test "$new_candidate" != "$candidate"
compose exec -T candidate test ! -e "/tmp/checkouts/$crash"
compose exec -T temporal sh -c 'test -s /home/temporal/history.sqlite'
test "$(compose ps -q temporal)" = "$temporal"
compose up -d --no-build --wait --wait-timeout 25 worker
new_worker=$(compose ps -q worker)
test "$new_worker" != "$worker"
{
    printf 'worker %s %s\n' "$worker" "$new_worker"
    printf 'candidate %s %s\n' "$candidate" "$new_candidate"
    printf 'temporal %s %s\n' "$temporal" "$(compose ps -q temporal)"
} > "$evidence/replacements.txt"
containers

control wait "$crash" > "$evidence/crash-result.json"
compose exec -T candidate git -C "/tmp/checkouts/$crash" rev-parse HEAD > "$evidence/crash-restored-head.txt"
compose exec -T candidate cat "/tmp/checkouts/$crash/final-record.txt" > "$evidence/crash-restored-untracked.txt"
control submit "$crash" > "$evidence/crash-duplicate-closed.json"

control submit "$fresh" > "$evidence/fresh-start.json"
control wait "$fresh" > "$evidence/fresh-result.json"
control fixture > "$evidence/fixture-after.json"

compose rm -sf worker candidate
test -z "$(compose ps -aq candidate)"
compose cp control:/evidence "$evidence/archive"
unset RECOVERY_ENVIRONMENT_DIGEST
compose run --no-deps --pull never -T -v "$evidence:/retained:ro" workspace \
    uv run --locked --offline python scripts/reopen_recovery_evidence.py /retained "$crash" "$fresh" \
    > "$evidence/reopened.txt"
cat "$evidence/reopened.txt"
containers

{
    printf 'elapsed_seconds=%s\n' "$(($(date +%s) - started))"
    printf 'containers_created=%s\n' "$(sort -u "$evidence/containers.txt" | wc -l | tr -d ' ')"
    printf 'worker_exit=137\nworker_replaced=true\ncandidate_replaced=true\nsame_temporal_database=true\n'
    printf 'model_calls=0\n'
    printf 'temporal_volume=%s_temporal-data\n' "$project"
    printf 'evidence_volume=%s_evidence\n' "$project"
    printf 'fixture_volume=%s_fixture-data\n' "$project"
} > "$evidence/run.txt"
cat "$evidence/run.txt"
