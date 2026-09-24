#!/usr/bin/env bash
# Run ops/validation/rerank_threshold_probe.py INSIDE the deployment's VPC and
# bring its report back into ops/validation/reports/.
#
# Why in the VPC. The probe samples passages from silver.document_passages
# and scores them through the deployed app.services.reranker. The Bedrock
# adapter is the code production calls, and it runs under the task role
# production uses. RDS is reachable only from inside the VPC, and the
# georag-fastapi task already has the DSN, the role and the adapter. So the
# probe goes to the task, as run_cohere_probe.sh does, and uses the same
# transport: a tarball staged in the bronze bucket, a one-off RunTask on
# georag-fastapi with PYTHONPATH=/app, and the report printed gzip+base64 in
# numbered chunks that this script reassembles from CloudWatch.
#
# What reaches CloudWatch: the report and the probe's own output. Both are
# scores, counts and parameters only. The probe never writes passage or query
# text (tests/test_rerank_threshold_probe.py pins that, and the runner test
# checks the log stream for it). The corpus text goes from RDS to Bedrock
# inside AWS and nowhere else.
#
# The report is written into ops/validation/reports/ ONLY when its verdict
# says it measured something. A run whose every Rerank call was denied still
# produces a well-formed report, and that is not evidence about the threshold.
#
# Usage (extra arguments go to the probe):
#   bash ops/rehearsal/run_rerank_threshold_probe.sh
#   bash ops/rehearsal/run_rerank_threshold_probe.sh --max-anchors 300 --seed 1
#   bash ops/rehearsal/run_rerank_threshold_probe.sh --harvest-since 2026-09-08
#   RERANK_PROBE_PAIRS=my_pairs.jsonl bash ops/rehearsal/run_rerank_threshold_probe.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
CLUSTER="${ECS_CLUSTER:-georag}"
REPORTS="${PROBE_REPORTS_DIR:-$REPO_ROOT/ops/validation/reports}"
PAIRS="${RERANK_PROBE_PAIRS:-}"
if [ "$#" -gt 0 ]; then
  PROBE_ARGS="$*"
else
  PROBE_ARGS="${RERANK_PROBE_ARGS:-}"
fi

for tool in aws jq tar gzip base64; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing required tool: $tool" >&2; exit 1; }
done
if [ -n "$PAIRS" ] && [ ! -f "$PAIRS" ]; then
  echo "pairs file not found: $PAIRS" >&2
  exit 1
fi

BUCKET=$(aws ecs describe-task-definition --task-definition georag-fastapi \
  --query 'taskDefinition.containerDefinitions[0].environment[?name==`AWS_BUCKET_BRONZE`].value | [0]' \
  --output text)
if [ -z "$BUCKET" ] || [ "$BUCKET" = "None" ]; then
  echo "could not read AWS_BUCKET_BRONZE from the georag-fastapi task definition" >&2
  exit 1
fi

NET=$(aws ecs describe-services --cluster "$CLUSTER" --services fastapi \
        --query 'services[0].networkConfiguration.awsvpcConfiguration')
SUBNETS=$(echo "$NET" | jq -r '.subnets|join(",")')
SG=$(echo "$NET" | jq -r '.securityGroups|join(",")')

# ── Stage the payload ───────────────────────────────────────────────────────
WORK=$(mktemp -d)
KEY="rehearsal/rerank-threshold/$(date +%s)-$$.tgz"
cleanup() {
  aws s3 rm "s3://$BUCKET/$KEY" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

cp "$REPO_ROOT/ops/validation/rerank_threshold_probe.py" "$REPO_ROOT/ops/validation/_probe_verdict.py" "$WORK/"
FILES="rerank_threshold_probe.py _probe_verdict.py"
if [ -n "$PAIRS" ]; then
  cp "$PAIRS" "$WORK/pairs.jsonl"
  FILES="$FILES pairs.jsonl"
fi
# shellcheck disable=SC2086 # FILES is a list of plain names
tar -C "$WORK" -czf "$WORK/payload.tgz" $FILES
aws s3 cp "$WORK/payload.tgz" "s3://$BUCKET/$KEY" >/dev/null
echo "staged probe payload at s3://$BUCKET/$KEY" >&2

# ── Run it in the VPC ───────────────────────────────────────────────────────
# PYTHONPATH is APPENDED to, not replaced: /app first so the probe imports the
# deployed app.services.reranker; anything already set stays reachable.
TASK_PY='
import base64, glob, gzip, io, os, shlex, subprocess, sys, tarfile, tempfile
import boto3
d = tempfile.mkdtemp(prefix="rerank-threshold-")
out = os.path.join(d, "reports")
blob = boto3.client("s3").get_object(Bucket=os.environ["PROBE_BUCKET"], Key=os.environ["PROBE_KEY"])["Body"].read()
tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz").extractall(d, filter="data")
app = os.environ.get("PROBE_APP_DIR", "/app")
env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in (app, os.environ.get("PYTHONPATH")) if p))
argv = [sys.executable, d + "/rerank_threshold_probe.py", "--out", out]
if os.path.exists(d + "/pairs.jsonl"):
    argv += ["--source", "pairs", "--pairs", d + "/pairs.jsonl"]
argv += shlex.split(os.environ.get("PROBE_ARGS", ""))
rc = subprocess.call(argv, cwd=app, env=env)
reports = sorted(glob.glob(os.path.join(out, "rerank_threshold_*.json")))
if not reports:
    print("PROBE_NO_REPORT rc=%d" % rc, flush=True)
    sys.exit(rc or 1)
raw = open(reports[-1], "rb").read()
b64 = base64.b64encode(gzip.compress(raw, mtime=0)).decode()
size = 8000
n = (len(b64) + size - 1) // size
print("PROBE_NAME " + os.path.basename(reports[-1]), flush=True)
for i in range(n):
    print("PROBE_B64 %d %d %s" % (i + 1, n, b64[i * size:(i + 1) * size]), flush=True)
print("PROBE_END rc=%d" % rc, flush=True)
'
OVR=$(jq -n --arg py "$TASK_PY" --arg b "$BUCKET" --arg k "$KEY" --arg a "$PROBE_ARGS" '{
  containerOverrides: [{
    name: "fastapi",
    command: ["python3", "-c", $py],
    environment: [{name: "PROBE_BUCKET", value: $b},
                  {name: "PROBE_KEY",    value: $k},
                  {name: "PROBE_ARGS",   value: $a}]}]}')
OVR_LEN=${#OVR}
if [ "$OVR_LEN" -gt 8192 ]; then
  echo "overrides payload is ${OVR_LEN} chars, over ECS RunTask's 8192 limit" >&2
  exit 1
fi

ARN=$(aws ecs run-task --cluster "$CLUSTER" --task-definition georag-fastapi \
      --launch-type FARGATE \
      --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}" \
      --overrides "$OVR" --query 'tasks[0].taskArn' --output text)
echo "probe task: $ARN (overrides ${OVR_LEN}/8192 chars). Waiting for it to stop..." >&2
# The stock waiter gives up after ten minutes, and a full corpus run can take
# longer. Letting it fail would fire the cleanup trap mid-run and lose the
# report, so keep waiting while the task is still alive.
until aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$ARN" 2>/dev/null; do
  STATUS=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$ARN" \
    --query 'tasks[0].lastStatus' --output text)
  [ "$STATUS" = "STOPPED" ] && break
  echo "  still ${STATUS}..." >&2
done

STREAM="fastapi/fastapi/${ARN##*/}"
EXIT_CODE=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)

aws logs filter-log-events --log-group-name /ecs/georag --log-stream-names "$STREAM" \
  --query 'events[].message' --output json > "$WORK/log.json"

NAME=$(jq -r '.[] | select(startswith("PROBE_NAME ")) | sub("^PROBE_NAME ";"")' "$WORK/log.json" | tail -1)
if [ -z "$NAME" ]; then
  echo "── the task produced no report (exit ${EXIT_CODE}); its own output: ──" >&2
  jq -r '.[]' "$WORK/log.json" | grep -v '^PROBE_B64 ' | tail -60 >&2
  exit 1
fi

jq -r '.[] | select(startswith("PROBE_B64 "))' "$WORK/log.json" \
  | awk '{print $2, $3, $4}' | sort -n -k1,1 -u > "$WORK/chunks"
EXPECTED=$(awk 'NR==1{print $2}' "$WORK/chunks")
GOT=$(wc -l < "$WORK/chunks")
if [ -z "$EXPECTED" ] || [ "$GOT" -ne "$EXPECTED" ]; then
  echo "report arrived incomplete: ${GOT}/${EXPECTED:-?} chunks in $STREAM" >&2
  exit 1
fi
awk '{printf "%s", $3}' "$WORK/chunks" | base64 -d | gunzip > "$WORK/$NAME"

jq -r '.[] | select(startswith("PROBE_B64 ") | not)' "$WORK/log.json" | grep -v '^PROBE_' | tail -12 >&2

VERIFIED=$(jq -r '.verdict.verified_anything // false' "$WORK/$NAME")
SUMMARY=$(jq -r '.verdict.summary // "(no verdict summary)"' "$WORK/$NAME")
REC=$(jq -r '.recommendation | "status=\(.status) current=\(.current) recommended=\(.recommended) band=\(.band) auc=\(.auc_on_vs_off_corpus)"' "$WORK/$NAME")
if [ "$VERIFIED" = "true" ]; then
  mkdir -p "$REPORTS"
  cp "$WORK/$NAME" "$REPORTS/$NAME"
  echo "MEASURED: wrote ops/validation/reports/$NAME" >&2
  echo "  $SUMMARY" >&2
  echo "  $REC" >&2
  echo "Commit it. The threshold itself changes only by a human edit to app/config.py that cites it." >&2
else
  KEEP="/tmp/$NAME"
  cp "$WORK/$NAME" "$KEEP"
  echo "NOT MEASURED: kept at $KEEP for diagnosis, NOT written to ops/validation/reports/" >&2
  echo "  $SUMMARY" >&2
  exit 1
fi
