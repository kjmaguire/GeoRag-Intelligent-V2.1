#!/usr/bin/env bash
# =============================================================================
# scripts/overnight_finalize_ingest.sh
#
# Post-ingest finalization. Runs AFTER scripts/overnight_uranium_ingest.sh
# finishes. Does the steps the cluster_runner doesn't:
#
#   1. Qdrant passage embedding — embed every new silver.document_passages
#      row produced by PDF ingestion.
#   2. Final silver/gold row counts + project list for the report.
#
# Idempotent — running twice re-embeds nothing already embedded.
#
# A KG-sync phase used to run first, pushing silver entities into Neo4j so
# §04i Layer 4 entity resolution would recognize the new Wyoming holes and
# operators. It was removed on 2026-09-15: it imported
# app.services.ingest.kg_sync, deleted with Neo4j on 2026-07-28, so it
# raised ImportError once per project and the loop swallowed it — the
# script printed "KG sync done: 0/N projects" and carried on to exit 0.
# Layer 4's graph half is permanently fail-open (CLAUDE.md hard rule 9);
# its Postgres half needs nothing from this script.
# =============================================================================

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WS_ID="${WS_ID:-a0000000-0000-0000-0000-000000000001}"

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
