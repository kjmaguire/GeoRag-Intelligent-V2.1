#!/usr/bin/env bash
# End-to-end tests for ops/rehearsal/run_rerank_threshold_probe.sh, with no AWS.
#
# Same shape as run_cohere_probe_test.sh: a fake `aws` on PATH stands in for
# S3, ECS and CloudWatch by executing the task's command locally. A fake boto3
# serves the staged tarball from a directory, and its Bedrock clients are
# ops/validation/tests/fake_bedrock_rerank.py. Only the AWS calls are fake.
# The runner, the in-task Python, the probe and the deployed reranker adapter
# (app.services.reranker via PYTHONPATH) all run for real.
#
# The in-task probe reads pairs from a staged file (RERANK_PROBE_PAIRS), which
# is a supported operator mode, because there is no Postgres here. The
# corpus-sampling path is covered by tests/test_rerank_threshold_probe.py.
#
# Run: bash scripts/tests/run_rerank_threshold_probe_test.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${TEST_PYTHON:-$REPO_ROOT/src/fastapi/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"
PASS=0
FAIL=0

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin" "$WORK/boto/botocore" "$WORK/s3" "$WORK/logs" "$WORK/reports"

# ── A boto3 for S3 plus the two Bedrock clients the adapter uses ────────────
cat > "$WORK/boto/boto3.py" <<'PYEOF'
import os
import sys


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class _S3:
    def get_object(self, Bucket, Key):
        with open(os.path.join(os.environ["FAKE_S3"], Bucket, Key), "rb") as fh:
            return {"Body": _Body(fh.read())}


def client(name, **_kwargs):
    if name == "s3":
        return _S3()
    sys.path.insert(0, os.environ["FAKE_RERANK_DIR"])
    import fake_bedrock_rerank

    return fake_bedrock_rerank.client(name)
PYEOF
: > "$WORK/boto/botocore/__init__.py"
cat > "$WORK/boto/botocore/config.py" <<'PYEOF'
class Config:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
PYEOF

# ── The fake `aws` ──────────────────────────────────────────────────────────
cat > "$WORK/bin/aws" <<'SHEOF'
#!/usr/bin/env bash
set -euo pipefail
args="$*"
case "$1 $2" in
  "ecs describe-task-definition") echo "fake-bronze" ;;
  "ecs describe-services") echo '{"subnets":["subnet-a"],"securityGroups":["sg-a"]}' ;;
  "ecs wait") : ;;
  "ecs describe-tasks")
    case "$args" in *lastStatus*) echo STOPPED ;; *) cat "$FAKE_LOGS/last-exit" ;; esac ;;
  "s3 cp")
    dest="${4#s3://}"; mkdir -p "$FAKE_S3/$(dirname "$dest")"; cp "$3" "$FAKE_S3/$dest" ;;
  "s3 rm") rm -f "$FAKE_S3/${3#s3://}" ;;
  "ecs run-task")
    overrides=""
    while [ $# -gt 0 ]; do [ "$1" = "--overrides" ] && overrides="$2"; shift; done
    printf '%s' "${#overrides}" > "$FAKE_LOGS/last-override-len"
    code=$(printf '%s' "$overrides" | jq -r '.containerOverrides[0].command[2]')
    # Values can contain spaces (PROBE_ARGS), so export them one by one.
    while IFS= read -r kv; do export "$kv"; done < <(printf '%s' "$overrides" \
      | jq -r '.containerOverrides[0].environment[] | "\(.name)=\(.value)"')
    set +e
    PROBE_APP_DIR="$FAKE_APP_DIR" PYTHONPATH="$FAKE_BOTO" \
      "$FAKE_PY" -c "$code" > "$FAKE_LOGS/out.txt" 2>&1
    echo $? > "$FAKE_LOGS/last-exit"
    set -e
    lines="$FAKE_LOGS/out.txt"
    if [ -n "${FAKE_DROP_CHUNK:-}" ]; then
      grep -v "^PROBE_B64 ${FAKE_DROP_CHUNK} " "$lines" > "$lines.tmp" || true
      mv "$lines.tmp" "$lines"
    fi
    jq -R . < "$lines" | jq -s . > "$FAKE_LOGS/events.json"
    echo "arn:aws:ecs:us-east-1:000000000000:task/georag/fake0001" ;;
  "logs filter-log-events") cat "$FAKE_LOGS/events.json" ;;
  *) echo "fake aws: unhandled: $args" >&2; exit 64 ;;
esac
SHEOF
chmod +x "$WORK/bin/aws"

export PATH="$WORK/bin:$PATH"
export FAKE_S3="$WORK/s3" FAKE_LOGS="$WORK/logs" FAKE_BOTO="$WORK/boto" FAKE_PY="$PY"
export FAKE_APP_DIR="$REPO_ROOT/src/fastapi"
export FAKE_RERANK_DIR="$REPO_ROOT/ops/validation/tests"
export PROBE_REPORTS_DIR="$WORK/reports"
# What the georag-fastapi task definition injects, in test-only form.
export FASTAPI_SERVICE_KEY="test-only-not-a-real-key-padding-to-32-bytes"
export POSTGRES_PASSWORD="test-only"
export RERANKER_BACKEND=bedrock
unset BEDROCK_RERANK_MODEL_ID RERANK_PROBE_ARGS

# ── A pair set built by the probe's own builder from a synthetic corpus ─────
PAIRS="$WORK/pairs.jsonl"
"$PY" - "$PAIRS" "$REPO_ROOT" <<'PYEOF'
import json, os, random, sys
sys.path.insert(0, os.path.join(sys.argv[2], "ops", "validation"))
import rerank_threshold_probe as probe

rng = random.Random(0)
suffixes = ["vein", "assay", "breccia", "sulphide", "schist", "intrusion",
            "fault", "grade", "collar", "adit", "shear", "porphyry"]
corpus = []
for d in range(10):
    prefix = "topic%dx" % d
    for p in range(6):
        sentences = []
        for _ in range(6):
            words = [prefix + s for s in rng.sample(suffixes, 8)]
            sentences.append("The " + " and ".join(words[:2]) + " with " + " ".join(words[2:]) + ".")
        corpus.append({"passage_id": "p%d-%d" % (d, p), "document_id": "d%d" % d, "text": " ".join(sentences)})
pairs, _ = probe.build_corpus_pairs(corpus, max_anchors=30, negatives_per_anchor=3, foreign_per_anchor=1)
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    for pair in pairs:
        fh.write(json.dumps({"query": pair.query, "passage": pair.passage,
                             "kind": pair.kind, "group": pair.group}) + "\n")
PYEOF
[ -s "$PAIRS" ] || { echo "could not build the pair set" >&2; exit 1; }
A_QUERY="$(head -1 "$PAIRS" | jq -r .query)"

run() {  # run <mode> [probe args...]
    local mode="$1"; shift
    FAKE_RERANK_MODE="$mode" RERANK_PROBE_PAIRS="$PAIRS" \
        bash "$REPO_ROOT/ops/rehearsal/run_rerank_threshold_probe.sh" "$@" 2>&1
}

check() {  # check <label> <condition-exit-status> [detail]
    if [ "$2" -eq 0 ]; then
        printf 'ok   %s\n' "$1"; PASS=$((PASS + 1))
    else
        printf 'FAIL %s\n%s\n' "$1" "${3:-}"; FAIL=$((FAIL + 1))
    fi
}

reports_written() { find "$WORK/reports" -name 'rerank_threshold_*.json' | wc -l; }
ARGS=(--min-pairs 20 --current-threshold 0.2 --stability-samples 4)

# ── 1. A healthy host: a measured, whole report lands in the reports dir ────
out="$(cd "$REPO_ROOT" && run separable "${ARGS[@]}")"; code=$?
check "a healthy run exits 0" "$code" "$out"
check "and says MEASURED" "$(printf '%s' "$out" | grep -q 'MEASURED: wrote' ; echo $?)" "$out"
check "exactly one report is written" "$([ "$(reports_written)" -eq 1 ]; echo $?)" "$(ls "$WORK/reports")"
report="$(find "$WORK/reports" -name 'rerank_threshold_*.json' | head -1)"
check "the report went through the deployed Bedrock adapter" \
    "$(jq -e '.reranker.version == "cohere-bedrock:cohere.rerank-v3-5:0" and .scoring.pairs_scored > 0' "$report" >/dev/null 2>&1; echo $?)" \
    "$(jq -c '{reranker, scoring}' "$report" 2>/dev/null)"
check "and carries a recommendation with its evidence" \
    "$(jq -e '.recommendation.status == "current_consistent" and .recommendation.recommended == 0.2 and (.recommendation.band | length) == 2' "$report" >/dev/null 2>&1; echo $?)" \
    "$(jq -c '.recommendation' "$report" 2>/dev/null)"
check "the probe arguments reached the task" \
    "$(jq -e '.recommendation.sample.on_topic > 0 and .stability.pairs_rescored == 4' "$report" >/dev/null 2>&1; echo $?)" \
    "$(jq -c '.stability' "$report" 2>/dev/null)"
check "no pair text reached the logs" \
    "$(! grep -qF "$A_QUERY" "$WORK/logs/events.json"; echo $?)"
check "the staged tarball is deleted afterwards" \
    "$([ -z "$(find "$WORK/s3" -type f)" ]; echo $?)" "$(find "$WORK/s3" -type f)"
check "the overrides fit ECS RunTask's 8192-char cap" \
    "$([ "$(cat "$WORK/logs/last-override-len")" -le 8192 ]; echo $?)" "$(cat "$WORK/logs/last-override-len")"

# ── 2. A denied role: a well-formed report of nothing, kept out of reports/ ─
rm -f "$WORK/reports"/*
out="$(cd "$REPO_ROOT" && run denied "${ARGS[@]}")"; code=$?
check "a denied run exits non-zero" "$([ "$code" -ne 0 ]; echo $?)" "$out"
check "and says NOT MEASURED" "$(printf '%s' "$out" | grep -q 'NOT MEASURED' ; echo $?)" "$out"
check "and writes nothing into the reports dir" "$([ "$(reports_written)" -eq 0 ]; echo $?)" "$(ls "$WORK/reports")"

# ── 3. A chunk lost in transit: refuse to write a truncated report ──────────
rm -f "$WORK/reports"/*
out="$(cd "$REPO_ROOT" && FAKE_DROP_CHUNK=1 run separable "${ARGS[@]}")"; code=$?
check "a lost chunk exits non-zero" "$([ "$code" -ne 0 ]; echo $?)" "$out"
check "and names the problem" "$(printf '%s' "$out" | grep -q 'incomplete' ; echo $?)" "$out"
check "and writes nothing" "$([ "$(reports_written)" -eq 0 ]; echo $?)" "$(ls "$WORK/reports")"

# ── 4. The CloudShell bundle carries and runs the SAME runner, no checkout ──
bash "$REPO_ROOT/ops/rehearsal/make_rerank_threshold_bundle.sh" > "$WORK/rerank.sh"
mkdir -p "$WORK/home" "$WORK/elsewhere"
out="$(cd "$WORK/elsewhere" && env -u PROBE_REPORTS_DIR HOME="$WORK/home" FAKE_RERANK_MODE=separable \
        RERANK_PROBE_PAIRS="$PAIRS" bash "$WORK/rerank.sh" "${ARGS[@]}" 2>&1)"; code=$?
check "the bundle runs from outside any checkout" "$code" "$out"
check "and leaves a measured report in ~/rerank-threshold-reports" \
    "$(ls "$WORK/home/rerank-threshold-reports"/rerank_threshold_*.json >/dev/null 2>&1; echo $?)" "$out"
check "and prints the report for copying" "$(printf '%s' "$out" | grep -q '"verified_anything": true'; echo $?)" "$out"

echo
echo "run_rerank_threshold_probe: ${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ]
