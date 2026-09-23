#!/usr/bin/env bash
# Run ops/validation/cohere_probe.py INSIDE the deployment's VPC, and bring
# its report back into ops/validation/reports/ for aws-preflight.sh A-11.
#
# Why not just run ops/validation/cohere_probe.sh? That needs two things an
# operator shell often lacks: egress to api.cohere.com and a copy of
# COHERE_API_KEY. The deployed georag-fastapi task already has both — it
# calls Cohere on every query, with the key injected from Secrets Manager.
# So the probe goes to the key rather than the key coming to the probe: the
# key never leaves AWS, and the run exercises the exact network path and
# credentials production uses.
#
# How it travels. The probe is ~40 KB and the fixture PDF ~18 KB, far past
# ECS RunTask's 8192-character override cap even gzipped, so neither can be
# inlined the way run_against_deployment.sh inlines its SQL. Instead they go
# as one tarball through the bronze bucket — the same route run_step5.sh
# uses for its PDF, readable by the shared `task` role (iam.tf). The task
# fetches it, runs the probe with PYTHONPATH=/app so the probe's
# _load_adapter checks the DEPLOYED llm_cohere, and prints the report
# gzip+base64 in numbered chunks. This script reassembles them from
# CloudWatch and deletes the staged object on the way out.
#
# The report is written into ops/validation/reports/ ONLY when its verdict
# says it verified something. A run where every call failed still produces a
# well-formed report (the probe degrades rather than raising), and committing
# that as evidence is exactly what A-11 was hardened against; such a report
# is kept under /tmp for diagnosis instead.
#
# What reaches CloudWatch: the probe's report and its own stdout/stderr —
# model replies to the probe's fixed prompts and Parse output of the public
# fixture PDF. The key is redacted by the probe (_redact); no workspace data
# is involved.
#
# Usage:
#   bash ops/rehearsal/run_cohere_probe.sh
#   PROBE_PDF=/path/to/scan.pdf PROBE_PAGES=1,3 bash ops/rehearsal/run_cohere_probe.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
CLUSTER="${ECS_CLUSTER:-georag}"
PDF="${PROBE_PDF:-$REPO_ROOT/src/fastapi/tests/fixtures/ocr/PLS-2024-Technical-Report.pdf}"
PAGES="${PROBE_PAGES:-1}"
REPORTS="${PROBE_REPORTS_DIR:-$REPO_ROOT/ops/validation/reports}"

for tool in aws jq tar gzip base64; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing required tool: $tool" >&2; exit 1; }
done
[ -f "$PDF" ] || { echo "PDF not found: $PDF (set PROBE_PDF=...)" >&2; exit 1; }

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
KEY="rehearsal/cohere-probe/$(date +%s)-$$.tgz"
cleanup() {
  aws s3 rm "s3://$BUCKET/$KEY" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

cp "$REPO_ROOT/ops/validation/cohere_probe.py" "$REPO_ROOT/ops/validation/_probe_verdict.py" "$WORK/"
cp "$PDF" "$WORK/probe.pdf"
tar -C "$WORK" -czf "$WORK/payload.tgz" cohere_probe.py _probe_verdict.py probe.pdf
aws s3 cp "$WORK/payload.tgz" "s3://$BUCKET/$KEY" >/dev/null
echo "staged probe payload at s3://$BUCKET/$KEY" >&2

# ── Run it in the VPC ───────────────────────────────────────────────────────
# `extractall(filter="data")` refuses absolute paths and links: the tarball
# is ours, but it sat in a bucket other principals can write to.
TASK_PY='
import base64, glob, gzip, io, os, subprocess, sys, tarfile, tempfile
import boto3
d = tempfile.mkdtemp(prefix="cohere-probe-")
out = os.path.join(d, "reports")
blob = boto3.client("s3").get_object(Bucket=os.environ["PROBE_BUCKET"], Key=os.environ["PROBE_KEY"])["Body"].read()
tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz").extractall(d, filter="data")
app = os.environ.get("PROBE_APP_DIR", "/app")
env = dict(os.environ, PYTHONPATH=app)
rc = subprocess.call([sys.executable, d + "/cohere_probe.py", "--pdf", d + "/probe.pdf",
                      "--pages", os.environ["PROBE_PAGES"], "--out", out], cwd=app, env=env)
reports = sorted(glob.glob(os.path.join(out, "cohere_probe_*.json")))
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
OVR=$(jq -n --arg py "$TASK_PY" --arg b "$BUCKET" --arg k "$KEY" --arg p "$PAGES" '{
  containerOverrides: [{
    name: "fastapi",
    command: ["python3", "-c", $py],
    environment: [{name: "PROBE_BUCKET", value: $b},
                  {name: "PROBE_KEY",    value: $k},
                  {name: "PROBE_PAGES",  value: $p}]}]}')
OVR_LEN=${#OVR}
if [ "$OVR_LEN" -gt 8192 ]; then
  echo "overrides payload is ${OVR_LEN} chars, over ECS RunTask's 8192 limit" >&2
  exit 1
fi

ARN=$(aws ecs run-task --cluster "$CLUSTER" --task-definition georag-fastapi \
      --launch-type FARGATE \
      --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}" \
      --overrides "$OVR" --query 'tasks[0].taskArn' --output text)
echo "probe task: $ARN (overrides ${OVR_LEN}/8192 chars) — waiting for it to stop..." >&2
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$ARN"

STREAM="fastapi/fastapi/${ARN##*/}"
EXIT_CODE=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)

# filter-log-events paginates for us; get-log-events would need a token loop.
aws logs filter-log-events --log-group-name /ecs/georag --log-stream-names "$STREAM" \
  --query 'events[].message' --output json > "$WORK/log.json"

NAME=$(jq -r '.[] | select(startswith("PROBE_NAME ")) | sub("^PROBE_NAME ";"")' "$WORK/log.json" | tail -1)
if [ -z "$NAME" ]; then
  echo "── the task produced no report (exit ${EXIT_CODE}); its own output: ──" >&2
  jq -r '.[]' "$WORK/log.json" | grep -v '^PROBE_B64 ' | tail -60 >&2
  exit 1
fi

# Reassemble in chunk order, and refuse a partial report rather than write one.
jq -r '.[] | select(startswith("PROBE_B64 "))' "$WORK/log.json" \
  | awk '{print $2, $3, $4}' | sort -n -k1,1 -u > "$WORK/chunks"
EXPECTED=$(awk 'NR==1{print $2}' "$WORK/chunks")
GOT=$(wc -l < "$WORK/chunks")
if [ -z "$EXPECTED" ] || [ "$GOT" -ne "$EXPECTED" ]; then
  echo "report arrived incomplete: ${GOT}/${EXPECTED:-?} chunks in $STREAM" >&2
  exit 1
fi
awk '{printf "%s", $3}' "$WORK/chunks" | base64 -d | gunzip > "$WORK/$NAME"

jq -r '.[] | select(startswith("PROBE_B64 ") | not)' "$WORK/log.json" | tail -40 >&2

VERIFIED=$(jq -r '.verdict.verified_anything // false' "$WORK/$NAME")
SUMMARY=$(jq -r '.verdict.summary // "(no verdict summary)"' "$WORK/$NAME")
if [ "$VERIFIED" = "true" ]; then
  mkdir -p "$REPORTS"
  cp "$WORK/$NAME" "$REPORTS/$NAME"
  echo "VERIFIED — wrote ops/validation/reports/$NAME" >&2
  echo "  $SUMMARY" >&2
  echo "Commit it; aws-preflight.sh A-11 reads the newest cohere_probe_*.json." >&2
else
  KEEP="/tmp/$NAME"
  cp "$WORK/$NAME" "$KEEP"
  echo "NOT VERIFIED — kept at $KEEP for diagnosis, NOT written to ops/validation/reports/" >&2
  echo "  $SUMMARY" >&2
  exit 1
fi
