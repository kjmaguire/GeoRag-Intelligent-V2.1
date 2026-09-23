#!/usr/bin/env bash
# End-to-end tests for ops/rehearsal/run_cohere_probe.sh, with no AWS and no
# Cohere key.
#
# The runner's job is plumbing — stage a tarball, run a task, reassemble a
# report from log lines, and decide whether it is evidence — and every one of
# those steps can fail in a way that still looks like a finished run. So the
# real script runs here against a fake `aws` on PATH that stands in for S3,
# ECS and CloudWatch by executing the task's command locally, with the task's
# Python talking to ops/validation/tests/fake_cohere.py instead of Cohere.
# Only the AWS calls are fake: the runner, the in-task Python, the probe and
# the deployed-adapter import (llm_cohere via PYTHONPATH) all run for real.
#
# Run: bash scripts/tests/run_cohere_probe_test.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="$REPO_ROOT/src/fastapi/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
PASS=0
FAIL=0

WORK="$(mktemp -d)"
FAKE_PID=""
trap '[ -n "$FAKE_PID" ] && kill "$FAKE_PID" 2>/dev/null; rm -rf "$WORK"' EXIT

mkdir -p "$WORK/bin" "$WORK/boto" "$WORK/s3" "$WORK/logs" "$WORK/reports"

# ── A boto3 whose only S3 call reads the fake bucket directory ─────────────
cat > "$WORK/boto/boto3.py" <<'PYEOF'
import os


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
    assert name == "s3", name
    return _S3()
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
  "ecs describe-tasks") cat "$FAKE_LOGS/last-exit" ;;
  "s3 cp")
    dest="${4#s3://}"; mkdir -p "$FAKE_S3/$(dirname "$dest")"; cp "$3" "$FAKE_S3/$dest" ;;
  "s3 rm") rm -f "$FAKE_S3/${3#s3://}" ;;
  "ecs run-task")
    # Pull the overrides out of the argument list, then run the container
    # command locally the way ECS would, capturing its output as log lines.
    overrides=""
    while [ $# -gt 0 ]; do [ "$1" = "--overrides" ] && overrides="$2"; shift; done
    printf '%s' "${#overrides}" > "$FAKE_LOGS/last-override-len"
    code=$(printf '%s' "$overrides" | jq -r '.containerOverrides[0].command[2]')
    envs=$(printf '%s' "$overrides" | jq -r '.containerOverrides[0].environment[] | "\(.name)=\(.value)"')
    set +e
    env $envs PROBE_APP_DIR="$FAKE_APP_DIR" PYTHONPATH="$FAKE_BOTO" \
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
export PROBE_REPORTS_DIR="$WORK/reports"
# What the georag-fastapi task definition injects, in test-only form. The
# probe imports app.config (via llm_cohere), which refuses to build Settings
# without these.
export COHERE_API_KEY="test-only-not-a-real-cohere-key"
export FASTAPI_SERVICE_KEY="test-only-not-a-real-key-padding-to-32-bytes"
export POSTGRES_PASSWORD="test-only"
unset COHERE_CHAT_MODEL COHERE_PARSE_MODEL

start_fake_cohere() {  # start_fake_cohere <mode>
    [ -n "$FAKE_PID" ] && kill "$FAKE_PID" 2>/dev/null
    local port
    port=$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
    FAKE_COHERE_MODE="$1" FAKE_COHERE_PORT="$port" \
        "$PY" "$REPO_ROOT/ops/validation/tests/fake_cohere.py" > "$WORK/fake_cohere.log" 2>&1 &
    FAKE_PID=$!
    export COHERE_BASE_URL="http://127.0.0.1:$port"
    for _ in $(seq 50); do
        "$PY" -c "import socket; socket.create_connection(('127.0.0.1',$port),0.2)" 2>/dev/null && return
        sleep 0.1
    done
    echo "fake cohere did not start" >&2; cat "$WORK/fake_cohere.log" >&2
}

run() { bash "$REPO_ROOT/ops/rehearsal/run_cohere_probe.sh" 2>&1; }

check() {  # check <label> <condition-exit-status> [detail]
    if [ "$2" -eq 0 ]; then
        printf 'ok   %s\n' "$1"; PASS=$((PASS + 1))
    else
        printf 'FAIL %s\n%s\n' "$1" "${3:-}"; FAIL=$((FAIL + 1))
    fi
}

reports_written() { find "$WORK/reports" -name 'cohere_probe_*.json' | wc -l; }

# ── 1. A healthy host: the report comes back whole, verified, and lands in
#      the reports dir where A-11 looks for it.
start_fake_cohere honest
out="$(run)"; code=$?
check "a healthy run exits 0" "$code" "$out"
check "and says VERIFIED" "$(printf '%s' "$out" | grep -q 'VERIFIED — wrote' ; echo $?)" "$out"
check "exactly one report is written" "$([ "$(reports_written)" -eq 1 ]; echo $?)" "$(ls "$WORK/reports")"
report="$(find "$WORK/reports" -name 'cohere_probe_*.json' | head -1)"
check "the report is the probe's own JSON, verdict intact" \
    "$(jq -e '.verdict.verified_anything == true and (.chat|type) == "object"' "$report" >/dev/null 2>&1; echo $?)" \
    "$(head -c 400 "$report" 2>/dev/null)"
check "the in-task import of the deployed adapter worked" \
    "$(jq -e '[.. | strings | select(test("NOTHING <-- adapter is wrong|ModuleNotFoundError"))] | length == 0' "$report" >/dev/null 2>&1; echo $?)"
check "the key never appears in what reached the logs" \
    "$(! grep -qF "$COHERE_API_KEY" "$WORK/logs/events.json"; echo $?)"
check "the staged tarball is deleted afterwards" \
    "$([ -z "$(find "$WORK/s3" -type f)" ]; echo $?)" "$(find "$WORK/s3" -type f)"
check "the overrides fit ECS RunTask's 8192-char cap" \
    "$([ "$(cat "$WORK/logs/last-override-len")" -le 8192 ]; echo $?)" "$(cat "$WORK/logs/last-override-len")"

# ── 2. Every call rejected. The probe still writes a well-formed report; the
#      runner must refuse to put it where A-11 would read it as evidence.
rm -f "$WORK/reports"/*
start_fake_cohere unauthorized
out="$(run)"; code=$?
check "an unauthorized run exits non-zero" "$([ "$code" -ne 0 ]; echo $?)" "$out"
check "and says NOT VERIFIED" "$(printf '%s' "$out" | grep -q 'NOT VERIFIED' ; echo $?)" "$out"
check "and writes nothing into the reports dir" "$([ "$(reports_written)" -eq 0 ]; echo $?)" "$(ls "$WORK/reports")"

# ── 3. A chunk lost in transit. Writing a truncated report would be a
#      corrupt file at best; refuse it.
rm -f "$WORK/reports"/*
start_fake_cohere honest
out="$(FAKE_DROP_CHUNK=1 run)"; code=$?
check "a lost chunk exits non-zero" "$([ "$code" -ne 0 ]; echo $?)" "$out"
check "and names the problem" "$(printf '%s' "$out" | grep -q 'incomplete' ; echo $?)" "$out"
check "and writes nothing" "$([ "$(reports_written)" -eq 0 ]; echo $?)" "$(ls "$WORK/reports")"

# ── 4. The CloudShell bundle. It must carry and run the SAME runner, with no
#      checkout: generate it, run it from an empty directory, and expect the
#      report where the bundle says it puts it.
start_fake_cohere honest
bash "$REPO_ROOT/ops/rehearsal/make_probe_bundle.sh" > "$WORK/probe.sh"
mkdir -p "$WORK/home" "$WORK/elsewhere"
out="$(cd "$WORK/elsewhere" && env -u PROBE_REPORTS_DIR HOME="$WORK/home" bash "$WORK/probe.sh" 2>&1)"; code=$?
check "the bundle runs from outside any checkout" "$code" "$out"
check "and leaves a verified report in ~/cohere-probe-reports" \
    "$(ls "$WORK/home/cohere-probe-reports"/cohere_probe_*.json >/dev/null 2>&1; echo $?)" "$out"
check "and prints the report for copying" "$(printf '%s' "$out" | grep -q '"verified_anything": true'; echo $?)" "$out"

echo
echo "run_cohere_probe: ${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ]
