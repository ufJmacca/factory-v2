#!/bin/sh
set -eu
umask 077

evidence_root=${EVIDENCE_ROOT:-.evidence}
mkdir -p "$evidence_root"
evidence_root=$(cd "$evidence_root" && pwd -P)
evidence=$(mktemp -d "$evidence_root/verify-fixtures.XXXXXX")
run_name=${evidence##*/}
printf 'Evidence directory %s\n' "$evidence"

git status --porcelain > "$evidence/git-status.txt"
if ! docker compose build workspace verifier > "$evidence/build.log" 2>&1; then
    cat "$evidence/build.log" >&2
    exit 1
fi

started=$(date +%s)
{
    printf 'factory_sha=%s\n' "$(git rev-parse HEAD)"
    printf 'runtime=%s\n' "$(docker version --format '{{.Server.Os}}/{{.Server.Arch}} Docker {{.Server.Version}}')"
    printf 'compose=%s\n' "$(docker compose version --short)"
    for service in workspace verifier; do
        image=$(docker compose config --images "$service")
        printf '%s_image=%s\n' "$service" "$(docker image inspect --format '{{.Id}}' "$image")"
    done
} > "$evidence/run.txt"

if ! docker compose run --name "$run_name-tests" --rm --no-deps --pull never -T \
    workspace uv run --locked --offline python -m unittest discover -s tests -v \
    > "$evidence/test.log" 2>&1; then
    cat "$evidence/test.log" >&2
    exit 1
fi
cat "$evidence/test.log"

docker compose run --name "$run_name-generate" --rm --no-deps --pull never -T \
    -v "$evidence:/evidence" workspace uv run --locked --offline \
    python benchmarks/records.py /evidence/submissions > "$evidence/heads.tsv"

cp grading/checks.json "$evidence/checks.json"
if command -v sha256sum >/dev/null 2>&1; then
    digest_line=$(sha256sum "$evidence/checks.json")
else
    digest_line=$(shasum -a 256 "$evidence/checks.json")
fi
check_digest=${digest_line%% *}
printf '%s\n' "$check_digest" > "$evidence/checks.sha256"

base=$(awk '$1 == "base" {print $2}' "$evidence/heads.tsv")
while read -r name expected_reason expected_status; do
    head=$(awk -v fixture="$name" '$1 == fixture {print $2}' "$evidence/heads.tsv")
    status=0
    docker compose run --name "$run_name-$name" --rm --no-deps --pull never -T \
        --interactive=false -v "$evidence/submissions/$name.bundle:/submission.bundle:ro" \
        verifier --base "$base" --head "$head" --bundle /submission.bundle \
        --check-digest "$check_digest" > "$evidence/$name.json" 2> "$evidence/$name.log" || status=$?
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$name" "$base" "$head" "$expected_reason" "$expected_status" "$status"
done > "$evidence/submissions.tsv" <<'CASES'
base check_failed:final_record_without_newline 1
correct checks_passed 0
mutant check_failed:empty_input 1
CASES

docker compose run --name "$run_name-reopen" --rm --no-deps --pull never -T \
    -v "$evidence:/evidence:ro" workspace uv run --locked --offline \
    python scripts/reopen_fixture_evidence.py /evidence > "$evidence/reopened.txt"
cat "$evidence/reopened.txt"

{
    printf 'fixture_seconds=%s\n' "$(($(date +%s) - started))"
    printf 'containers=%s-tests,%s-generate,%s-base,%s-correct,%s-mutant,%s-reopen\n' \
        "$run_name" "$run_name" "$run_name" "$run_name" "$run_name" "$run_name"
    printf 'model_calls=0\nexternal_service_writes=0\n'
    printf 'tests=passed\nevidence=verified\n'
} >> "$evidence/run.txt"
cat "$evidence/run.txt"
