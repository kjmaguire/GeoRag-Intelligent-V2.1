#!/usr/bin/env bash
# Rehearsal step 5 — ingest a document, then prove a query streams, cites a
# REAL chunk, and terminates cleanly.
#
# Two phases because they have very different shapes. `ingest` kicks off a
# Hatchet workflow and returns immediately; the parse/chunk/embed/index chain
# then runs for minutes. `query` is synchronous. Running them as one command
# would mean guessing a sleep, and a guessed sleep is how you get a red run
# that means "not finished yet".
#
#   bash ops/rehearsal/run_step5.sh ingest   # upload + trigger, then WAIT
#   bash ops/rehearsal/run_step5.sh status   # are the passages indexed yet?
#   bash ops/rehearsal/run_step5.sh query    # the actual step-5 assertion
#
# Reads PROJECT_ID / WORKSPACE_ID from the environment, defaulting to the
# Meridian tenant from seed_multitenant_corpus.sql so step 5 and step 6 share
# one corpus rather than each building their own.
set -euo pipefail

CLUSTER="${ECS_CLUSTER:-georag}"
ACTION="${1:-query}"
PROJECT_ID="${PROJECT_ID:-aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa}"
WORKSPACE_ID="${WORKSPACE_ID:-11111111-aaaa-4aaa-8aaa-111111111111}"
QUESTION="${REHEARSAL_QUESTION:-What does this project contain?}"

NET=$(aws ecs describe-services --cluster "$CLUSTER" --services fastapi \
        --query 'services[0].networkConfiguration.awsvpcConfiguration')
SUBNETS=$(echo "$NET" | jq -r '.subnets|join(",")')
SG=$(echo "$NET" | jq -r '.securityGroups|join(",")')

run_task() {  # run_task <name> <json containerOverrides>
  local label="$1" overrides="$2"
  local arn
  arn=$(aws ecs run-task --cluster "$CLUSTER" --task-definition georag-fastapi \
        --launch-type FARGATE \
        --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}" \
        --overrides "$overrides" --query 'tasks[0].taskArn' --output text)
  echo "${label} task: ${arn}" >&2
  aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$arn"
  echo "── ${label} output ──────────────────────────────────────" >&2
  aws logs get-log-events --log-group-name /ecs/georag \
    --log-stream-name "fastapi/fastapi/${arn##*/}" \
    --query 'events[].message' --output text 2>/dev/null | tr '\t' '\n' \
    || echo "(could not read the task log)"
  echo "─────────────────────────────────────────────────────────" >&2
  aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$arn" \
    --query 'tasks[0].containers[0].exitCode' --output text
}

case "$ACTION" in
  ingest)
    # The fixture PDF ships in the image at /app/tests/fixtures/ocr/, so it is
    # uploaded from inside the task rather than from here — no local copy, and
    # no dependency on this shell having the repo checked out.
    OVR=$(jq -n --arg pid "$PROJECT_ID" --arg wid "$WORKSPACE_ID" '{
      containerOverrides: [{
        name: "fastapi",
        command: ["python3","-c", ("
import json,os,urllib.request,uuid,boto3
pid, wid = \"" + $pid + "\", \"" + $wid + "\"
src = \"/app/tests/fixtures/ocr/PLS-2024-Technical-Report.pdf\"
key = f\"reports/{pid}/rehearsal-{uuid.uuid4()}.pdf\"
bucket = os.environ[\"AWS_BUCKET_BRONZE\"]
boto3.client(\"s3\").upload_file(src, bucket, key)
size = os.path.getsize(src)
print(f\"uploaded s3://{bucket}/{key} ({size} bytes)\")
body = json.dumps({\"workspace_id\": wid, \"project_id\": pid, \"minio_key\": key,
                   \"file_size\": size, \"correlation_token\": str(uuid.uuid4())}).encode()
req = urllib.request.Request(
    os.environ[\"FASTAPI_INTERNAL_URL\"].rstrip(\"/\") + \"/internal/v1/shadow/ingest_pdf/trigger\",
    data=body, headers={\"Content-Type\":\"application/json\",
                        \"X-Service-Key\": os.environ[\"FASTAPI_SERVICE_KEY\"]})
with urllib.request.urlopen(req, timeout=60) as r:
    print(\"trigger:\", r.status, r.read().decode()[:300])
")]}]}')
    run_task ingest "$OVR"
    echo
    echo "Ingestion DISPATCHED, not finished. Hatchet now parses, chunks," >&2
    echo "embeds and indexes. Poll with: bash $0 status" >&2
    ;;

  status)
    # Scoped by workspace_id, NOT project_id: silver.document_passages has no
    # project_id column at all — it reaches a project only through
    # document_id -> silver.reports.report_id. Each rehearsal tenant owns one
    # project, so workspace scoping answers the same question without the
    # join, using only columns the schema actually has.
    #
    # `embedding_id` is the embedded marker. There is no `embedded_at`; the
    # first draft of this script invented one and would have died on "column
    # does not exist" against the live database.
    OVR=$(jq -n --arg wid "$WORKSPACE_ID" '{
      containerOverrides: [{
        name: "fastapi",
        command: ["python3","-c", ("
import asyncio, asyncpg
from app.db.dsn import build_dsn
async def go():
    c = await asyncpg.connect(build_dsn(scheme=\"postgresql\", include_sslmode=True))
    await c.execute(\"SELECT set_config($1,$2,false)\", \"app.workspace_id\", \"" + $wid + "\")
    row = await c.fetchrow(
        \"SELECT count(*) AS n, count(embedding_id) AS e \"
        \"FROM silver.document_passages WHERE workspace_id::text = $1\",
        \"" + $wid + "\")
    print(f\"passages={row[0]} embedded={row[1]}\")
    print(\"READY\" if row[1] else \"NOT READY - ingestion still running, re-check\")
    await c.close()
asyncio.run(go())
")]}]}')
    run_task status "$OVR"
    ;;

  query)
    OVR=$(jq -n --arg pid "$PROJECT_ID" --arg wid "$WORKSPACE_ID" --arg q "$QUESTION" '{
      containerOverrides: [{
        name: "fastapi",
        command: ["python3","/app/scripts/ops/step5_answer_path.py"],
        environment: [
          {name:"PROD_SMOKE_PROJECT_ID",   value:$pid},
          {name:"PROD_SMOKE_WORKSPACE_ID", value:$wid},
          {name:"REHEARSAL_QUESTION",      value:$q}
        ]}]}')
    CODE=$(run_task step5 "$OVR")
    echo "exit code: ${CODE}"
    [ "$CODE" = "0" ] || { echo "::error::step 5 FAILED"; exit 1; }
    echo "STEP 5 OK"
    ;;
  *) echo "usage: $0 {ingest|status|query}" >&2; exit 2 ;;
esac
