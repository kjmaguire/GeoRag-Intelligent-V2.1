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
    # The PDF is uploaded from HERE, not from inside the task.
    #
    # An earlier version did boto3.upload_file("/app/tests/fixtures/ocr/...")
    # inside the container, commented "the fixture PDF ships in the image".
    # It does not. docker/fastapi.Dockerfile.dockerignore excludes
    # **/tests/fixtures, so the file is not even in the build context — the
    # live run on 2026-09-18 died with FileNotFoundError. Uploading from the
    # shell needs no image change and no CD cycle.
    PDF="${PDF:-src/fastapi/tests/fixtures/ocr/PLS-2024-Technical-Report.pdf}"
    if [ ! -f "$PDF" ]; then
      echo "PDF not found: $PDF (set PDF=/path/to/file.pdf)" >&2
      exit 1
    fi

    BUCKET=$(aws ecs describe-task-definition --task-definition georag-fastapi \
      --query 'taskDefinition.containerDefinitions[0].environment[?name==`AWS_BUCKET_BRONZE`].value | [0]' \
      --output text)
    if [ -z "$BUCKET" ] || [ "$BUCKET" = "None" ]; then
      echo "could not read AWS_BUCKET_BRONZE from the georag-fastapi task definition" >&2
      exit 1
    fi
    KEY="reports/${PROJECT_ID}/rehearsal-$(date +%s)-$$.pdf"
    SIZE=$(stat -c%s "$PDF")
    echo "uploading $PDF ($SIZE bytes) -> s3://$BUCKET/$KEY" >&2
    aws s3 cp "$PDF" "s3://$BUCKET/$KEY" >/dev/null

    OVR=$(jq -n --arg pid "$PROJECT_ID" --arg wid "$WORKSPACE_ID" \
                --arg k "$KEY" --arg sz "$SIZE" '{
      containerOverrides: [{
        name: "fastapi",
        command: ["python3","-c", ("
import json,os,urllib.request,uuid
body = json.dumps({\"workspace_id\": \"" + $wid + "\", \"project_id\": \"" + $pid + "\",
                   \"minio_key\": os.environ[\"REHEARSAL_S3_KEY\"],
                   \"file_size\": int(os.environ[\"REHEARSAL_FILE_SIZE\"]),
                   \"correlation_token\": str(uuid.uuid4())}).encode()
req = urllib.request.Request(
    os.environ[\"FASTAPI_INTERNAL_URL\"].rstrip(\"/\") + \"/internal/v1/shadow/ingest_pdf/trigger\",
    data=body, headers={\"Content-Type\":\"application/json\",
                        \"X-Service-Key\": os.environ[\"FASTAPI_SERVICE_KEY\"]})
with urllib.request.urlopen(req, timeout=60) as r:
    print(\"trigger:\", r.status, r.read().decode()[:300])
")],
        environment: [{name:"REHEARSAL_S3_KEY",value:$k},
                      {name:"REHEARSAL_FILE_SIZE",value:$sz}]}]}')
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
