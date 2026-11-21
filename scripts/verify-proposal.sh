#!/bin/sh
set -eu
umask 077

: "${P05_PRODUCT_ARCHIVE:?Set this to the retained P04 live directory}"
: "${P05_CODEX_ARCHIVE:?Set this to the retained P03 live directory}"
product=$(cd "$P05_PRODUCT_ARCHIVE" && pwd -P)
codex=$(cd "$P05_CODEX_ARCHIVE" && pwd -P)
baseline=63ff5b1264197c5f9e92e7d6fb32d78528064cf2
skill=.agents/skills/delivery-evidence/SKILL.md
evidence_base=${EVIDENCE_ROOT:-${TMPDIR:-/tmp}}
mkdir -p "$evidence_base"
scratch=$(mktemp -d "$(cd "$evidence_base" && pwd -P)/verify-proposal.XXXXXX")
printf 'Evidence directory %s\n' "$scratch"
mkdir "$scratch/proposals" "$scratch/candidate"
git archive --format=tar "$baseline" -- "$skill" agents.toml > "$scratch/baseline.tar"
test "$(git get-tar-commit-id < "$scratch/baseline.tar")" = "$baseline"

protected_snapshot() {
    tar -cf "$1" agents.toml .agents grading benchmarks
}

git rev-parse HEAD > "$scratch/factory-sha.txt"
git status --porcelain > "$scratch/git-status.txt"
protected_snapshot "$scratch/protected.before.tar"

compose() {
    docker compose -p "factory-p05-$(basename "$scratch" | tr '[:upper:].' '[:lower:]-')" "$@"
}

if ! compose build workspace > "$scratch/build.log" 2>&1; then
    cat "$scratch/build.log" >&2
    exit 1
fi

ingest() {
    compose run --rm --no-deps --pull never -T \
        -v "$PWD:/workspace:ro" -v "$product:/p04:ro" -v "$codex:/p03:ro" \
        -v "$scratch/baseline.tar:/baseline/inputs.tar:ro" -v "$scratch/proposals:/proposals" \
        -e PYTHONPATH=src workspace uv run --locked --offline python -B -m factory.proposals \
        --product-archive /p04 --codex-archive /p03 --baseline-archive /baseline/inputs.tar \
        --baseline "$baseline" --output /proposals
}

ingest > "$scratch/first.txt"
set -- "$scratch/proposals/"*.json
test "$#" -eq 1 && test -f "$1"
proposal=$1
cp "$proposal" "$scratch/first.json"
ingest > "$scratch/second.txt"
set -- "$scratch/proposals/"*.json
test "$#" -eq 1 && test "$1" = "$proposal"
cmp "$scratch/first.txt" "$scratch/second.txt"
cmp "$scratch/first.json" "$proposal"

jq -ej '.candidate.diff' "$proposal" > "$scratch/candidate.patch"
jq -eS '.candidate.manifest' "$proposal" > "$scratch/candidate-manifest.json"
jq -eS '.experiment_request' "$proposal" > "$scratch/experiment-request.json"
jq -eS '.proposal' "$proposal" > "$scratch/proposal.json"

sha256() {
    shasum -a 256 "$1" | awk '{print $1}'
}

jq -ecS '.' "$scratch/candidate-manifest.json" > "$scratch/candidate-manifest.canonical.json"
jq -ecS '.' "$scratch/proposal.json" > "$scratch/proposal.canonical.json"
manifest_digest=$(sha256 "$scratch/candidate-manifest.canonical.json")
test "$manifest_digest" = "$(jq -er '.candidate_manifest_sha256' "$scratch/proposal.json")"
test "$manifest_digest" = "$(jq -er '.candidate_manifest_sha256' "$scratch/experiment-request.json")"
test "$(sha256 "$scratch/proposal.canonical.json")" = "$(jq -er '.proposal_sha256' "$scratch/experiment-request.json")"
test "$(jq -er '.experiment_request_id' "$scratch/proposal.json")" = "$(jq -er '.id' "$scratch/experiment-request.json")"

for file in candidate.patch candidate-manifest.json proposal.json experiment-request.json; do
    cmp "docs/p05/$file" "$scratch/$file"
done

tar -xf "$scratch/baseline.tar" -C "$scratch/candidate"
test "$(sha256 "$scratch/candidate/$skill")" = "$(jq -er '.baseline.skill_sha256' "$scratch/proposal.json")"
git -C "$scratch/candidate" init -q
git -C "$scratch/candidate" apply --check "$scratch/candidate.patch"
git -C "$scratch/candidate" apply "$scratch/candidate.patch"
test "$(sha256 "$scratch/candidate/$skill")" = "$(jq -er '.sha256' "$scratch/candidate-manifest.json")"
test "$(sha256 "$scratch/candidate.patch")" = "$(jq -er '.diff_sha256' "$scratch/candidate-manifest.json")"
test "$(sha256 "$scratch/candidate/agents.toml")" = "$(jq -er '.baseline.selector_sha256' "$scratch/proposal.json")"
test "$(git -C "$scratch/candidate" apply --numstat "$scratch/candidate.patch" | cut -f3-)" = "$skill"

git status --porcelain > "$scratch/git-status.after.txt"
protected_snapshot "$scratch/protected.after.tar"
cmp "$scratch/git-status.txt" "$scratch/git-status.after.txt"
cmp "$scratch/protected.before.tar" "$scratch/protected.after.tar"
rm "$scratch/protected.before.tar" "$scratch/protected.after.tar"

jq -ec '{id, diagnosis, comparison_diagnosis, reproduction}' "$scratch/proposal.json"
printf '%s\n' 'ingestions=2 proposals=1 candidate_files=1 protected_writes=0 model_calls=0 external_service_writes=0'
