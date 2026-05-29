#!/usr/bin/env bash
# =============================================================================
# scripts/overnight_finalize_ingest.sh
#
# Post-ingest finalization. Runs AFTER scripts/overnight_uranium_ingest.sh
# finishes. Does the steps the cluster_runner doesn't:
#
#   1. KG sync — push silver entities into Neo4j (one node per project +
#      per drillhole + per report). Enables §04i Layer 4 entity
#      resolution to recognize the new Wyoming holes/operators.
#   2. Qdrant passage embedding — embed every new silver.document_passages
#      row produced by PDF ingestion.
#   3. Final silver/gold row counts + project list for the report.
#
# Idempotent — running twice is a no-op on already-synced projects.
# =============================================================================

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WS_ID="${WS_ID:-a0000000-0000-0000-0000-000000000001}"

echo "==> Phase: KG sync for every project"
PROJECTS=$(docker exec georag-postgresql psql -U georag -d georag -tA -c "
    SELECT project_id::text || '|' || slug
      FROM silver.projects
     WHERE workspace_id = '${WS_ID}'
     ORDER BY project_name;")

count=0
synced=0
while IFS='|' read -r PID SLUG; do
    count=$((count+1))
    [ -z "$PID" ] && continue
    echo "  [$count] sync $SLUG ($PID)"
    if docker exec -e NEO4J_USER="${NEO4J_USER:-neo4j}" georag-fastapi python3 -c "
import asyncio, asyncpg, os, sys
sys.path.insert(0, '/app')
# kg_sync uses NEO4J_USER; map from NEO4J_USERNAME if only that is set
if 'NEO4J_USER' not in os.environ and 'NEO4J_USERNAME' in os.environ:
    os.environ['NEO4J_USER'] = os.environ['NEO4J_USERNAME']
from app.services.ingest.kg_sync import sync_silver_project_to_neo4j

async def main():
    dsn = ('postgres://'+os.environ.get('POSTGRES_USER','georag')
           + ':'+os.environ['POSTGRES_PASSWORD']
           + '@postgresql:5432/'+os.environ.get('POSTGRES_DB','georag'))
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        r = await sync_silver_project_to_neo4j(conn, project_id='$PID')
        print(f'  sync result: {r}')
    finally:
        await conn.close()

asyncio.run(main())
" 2>&1 | tail -3; then
        synced=$((synced+1))
    fi
done <<< "$PROJECTS"

echo
echo "==> KG sync done: $synced/$count projects"
echo

echo "==> Phase: trigger Qdrant embedding for new passages"
# embed_pending_passages walks silver.document_passages for the workspace
# and pushes any un-embedded rows into the georag_reports Qdrant
# collection. Lazy-loads the BGE model + Qdrant client.
docker exec -e WS_ID="$WS_ID" georag-fastapi python3 -c "
import asyncio, os, sys
sys.path.insert(0, '/app')
from app.services.ingest.passage_embedder import embed_pending_passages

async def main():
    r = await embed_pending_passages(workspace_id=os.environ['WS_ID'], batch_size=64)
    print(f'  embedding result: {r}')

asyncio.run(main())
" 2>&1 | tail -5

echo
echo "==> Final silver state"
docker exec georag-postgresql psql -U georag -d georag -c "
SELECT 'projects' AS table_name, COUNT(*) FROM silver.projects WHERE workspace_id = '${WS_ID}'
UNION ALL SELECT 'collars', COUNT(*) FROM silver.collars c JOIN silver.projects p ON p.project_id=c.project_id WHERE p.workspace_id = '${WS_ID}'
UNION ALL SELECT 'well_log_curves', COUNT(*) FROM silver.well_log_curves
UNION ALL SELECT 'reports', COUNT(*) FROM silver.reports WHERE workspace_id = '${WS_ID}'
UNION ALL SELECT 'document_passages', COUNT(*) FROM silver.document_passages
UNION ALL SELECT 'lithology_logs', COUNT(*) FROM silver.lithology_logs WHERE workspace_id = '${WS_ID}'
UNION ALL SELECT 'samples', COUNT(*) FROM silver.samples WHERE workspace_id = '${WS_ID}'
ORDER BY table_name;"

echo
echo "==> Project list"
docker exec georag-postgresql psql -U georag -d georag -c "
SELECT slug, project_name, region, company,
       (SELECT COUNT(*) FROM silver.collars WHERE project_id = p.project_id) AS collars
  FROM silver.projects p
 WHERE workspace_id = '${WS_ID}'
 ORDER BY collars DESC;"

echo
echo "==> Finalize complete: $(date -u +%FT%TZ)"
