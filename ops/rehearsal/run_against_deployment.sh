#!/usr/bin/env bash
# Seed the two-tenant corpus into the LIVE database and verify the Martin
# tenant fence against it. Rehearsal step 6.
#
# Usage:
#   bash ops/rehearsal/run_against_deployment.sh seed
#   bash ops/rehearsal/run_against_deployment.sh verify
#   bash ops/rehearsal/run_against_deployment.sh teardown
#
# This is a REHEARSAL tool. `seed` writes two workspaces, two projects and six
# collars, all slug-prefixed `rehearsal-`; `teardown` removes exactly those
# rows and asserts nothing is left. Do not point it at a database carrying
# real tenant data without reading the teardown first.
#
# ── WHY THIS RUNS ON THE FASTAPI TASK DEFINITION ────────────────────────────
#
# The first version of this script ran on `georag-migrate` and piped SQL into
# `psql "$DATABASE_URL"`. It was written from the shape of the Qdrant bootstrap
# in deploy/aws/README.md Step 4 and never executed. It could not have worked,
# for three independent reasons, each found by inspection on 2026-09-18 before
# the first live run:
#
#   1. NO psql BINARY. docker/laravel.Dockerfile installs `libpq-dev` — the
#      client *library*, for PHP's pdo_pgsql/pgsql extensions. The `psql`
#      program ships in `postgresql-client`, which is not installed in either
#      stage. The command would have died on "psql: not found".
#
#   2. NO DATABASE_URL. deploy/aws/terraform/config.tf defines DATABASE_URL
#      only for martin and hatchet. The migrate task gets MIGRATE_DB_HOST /
#      _PORT / _USERNAME / _PASSWORD and the laravel-octane environment; the
#      variable this script dereferenced is simply unset there.
#
#   3. THE OVERRIDE WAS A SILENT NO-OP. The migrate task definition sets
#      entryPoint = ["/bin/sh","-c"] (deploy/aws/terraform/services.tf:652).
#      An ECS containerOverrides.command REPLACES CMD but cannot change
#      entryPoint, so the final argv was
#          /bin/sh -c sh -lc "printf ... | psql ..."
#      which runs the *program* `sh` with the rest as positional parameters,
#      reads nothing, and EXITS 0. Verified by running that exact argv. The
#      old script read the container exit code as the verdict, so it would
#      have printed "verify_tenant_fence.sql OK" having executed no SQL at
#      all — the same absence-as-success shape this rehearsal has already
#      produced three times (the loopback smoke check, the never-authenticated
#      answer-path check, and the probe verdict that judged all-403s a pass).
#
# A fourth defect gated the others: the verify override serialised to 8316
# characters against ECS RunTask's 8192 limit, so today it fails loudly at the
# API call. That is the only reason the vacuous pass had not yet happened —
# and trimming the SQL, the obvious fix, is exactly what would have unmasked
# it. The SQL now travels gzip+base64 (roughly a quarter of the size), so the
# limit stops being something a future edit can drift back into.
#
# The fastapi task definition has no entryPoint, ships python3 + asyncpg +
# app.db.dsn, and its WORKDIR is /app so `from app.db...` resolves. It is the
# only image in this deployment that can talk to Postgres at all.
#
# THE DRIVER IS INLINE rather than baked into the image on purpose: it works
# against the image that is deployed RIGHT NOW. Adding a script under
# src/fastapi/scripts/ops/ would have made step 6 wait on a ~25-minute CD
# cycle for a file whose whole job is to run ad-hoc SQL.
set -euo pipefail

CLUSTER="${ECS_CLUSTER:-georag}"
ACTION="${1:-verify}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Acknowledgements each file must emit before its run counts as a pass. These
# are the anti-vacuous-pass contract: the SQL raises a tagged NOTICE at the end
# of every section, and the driver fails when it has not seen them all. A
# driver that executes nothing scores 0, not a clean exit.
case "$ACTION" in
  seed)     SQL_FILE=seed_multitenant_corpus.sql;     EXPECT=1 ;;
  verify)   SQL_FILE=verify_tenant_fence.sql;         EXPECT=5 ;;
  teardown) SQL_FILE=teardown_multitenant_corpus.sql; EXPECT=1 ;;
  *) echo "usage: $0 {seed|verify|teardown}" >&2; exit 2 ;;
esac

command -v jq >/dev/null || { echo "jq is required" >&2; exit 2; }

# Subnets/SG from a running service rather than terraform: the 2026-09-18
# rehearsal found no operator terraform environment existed anywhere, and this
# needs none.
NET=$(aws ecs describe-services --cluster "$CLUSTER" --services fastapi \
        --query 'services[0].networkConfiguration.awsvpcConfiguration')
SUBNETS=$(echo "$NET" | jq -r '.subnets|join(",")')
SG=$(echo "$NET" | jq -r '.securityGroups|join(",")')
if [ -z "$SUBNETS" ] || [ "$SUBNETS" = "null" ]; then
  echo "could not read the fastapi service's subnets from cluster ${CLUSTER}" >&2
  exit 1
fi

# -n: no mtime in the gzip header, so the same SQL always produces the same
# payload and two runs are diffable.
SQL_B64=$(gzip -n -9 -c "${HERE}/${SQL_FILE}" | base64 -w0)

PY=$(cat <<PYEOF
import asyncio, base64, re, sys, zlib
import asyncpg
from app.db.dsn import build_dsn

SQL = zlib.decompress(base64.b64decode("${SQL_B64}"), 31).decode()
EXPECT = ${EXPECT}
LABEL = "${SQL_FILE}"

seen = []

def _log(_conn, msg):
    # asyncpg hands NOTICE/WARNING here as a PostgresLogMessage. Printing as
    # they arrive means a failure mid-script still shows how far it got.
    text = getattr(msg, "message", str(msg))
    seen.append(text)
    print(text, flush=True)

async def main():
    conn = await asyncpg.connect(build_dsn(scheme="postgresql", include_sslmode=True))
    conn.add_log_listener(_log)
    try:
        # No arguments, so asyncpg uses the simple query protocol and the
        # multi-statement script runs as written.
        await conn.execute(SQL)
    finally:
        await conn.close()

asyncio.run(main())

acks = sorted({m.group(0) for line in seen
               for m in [re.search(r"\[(?:CHECK-OK \d|SEED-OK|TEARDOWN-OK)\]", line)] if m})
print("acknowledgements: %d/%d %s" % (len(acks), EXPECT, acks), flush=True)
if len(acks) != EXPECT:
    print("::error::%s: %d of %d checks acknowledged - the script did not fully "
          "execute, so this is NOT a pass" % (LABEL, len(acks), EXPECT), flush=True)
    sys.exit(1)
print("%s OK" % LABEL, flush=True)
PYEOF
)

OVERRIDES=$(jq -n --arg py "$PY" '{containerOverrides:[{name:"fastapi",command:["python3","-c",$py]}]}')

# Fail here rather than at the API, with the number that matters.
OVR_LEN=${#OVERRIDES}
if [ "$OVR_LEN" -ge 8192 ]; then
  echo "::error::overrides payload is ${OVR_LEN} chars, over ECS RunTask's 8192 limit." >&2
  echo "Trim ${SQL_FILE} or move the driver into the image; do NOT work around this by" >&2
  echo "dropping checks, which is how a verification turns into a vacuous pass." >&2
  exit 1
fi

echo "running ops/rehearsal/${SQL_FILE} against ${CLUSTER} (overrides ${OVR_LEN}/8192 chars)..."

TASK_ARN=$(aws ecs run-task --cluster "$CLUSTER" \
  --task-definition georag-fastapi \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}" \
  --overrides "$OVERRIDES" \
  --query 'tasks[0].taskArn' --output text)

echo "task: $TASK_ARN"
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN"

EXIT_CODE=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)

echo "-- output ------------------------------------------------"
aws logs get-log-events --log-group-name /ecs/georag \
  --log-stream-name "fastapi/fastapi/${TASK_ARN##*/}" \
  --query 'events[].message' --output text 2>/dev/null | tr '\t' '\n' \
  || echo "(could not read the task log)"
echo "----------------------------------------------------------"

# An exit code of None means the container never started (image pull, ENI,
# capacity). Treating that as anything other than a failure would be the same
# absence-as-success mistake the header describes.
echo "exit code: ${EXIT_CODE}"
[ "$EXIT_CODE" = "0" ] || { echo "::error::${SQL_FILE} FAILED (exit ${EXIT_CODE})"; exit 1; }
echo "${SQL_FILE} OK"
