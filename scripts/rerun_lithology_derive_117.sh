#!/usr/bin/env bash
# Re-derive lithology intervals for the 117 newly-ingested Wyoming projects.
# Runs derive_intervals.py per project_id inside the fastapi container.
set -uo pipefail

WS_ID="${WS_ID:-a0000000-0000-0000-0000-000000000001}"
OUTLOG="docs/lithology_derive_rerun.log"

echo "==> Lithology derive re-run starts $(date -u +%FT%TZ)" | tee "$OUTLOG"

# Skip the original Cameco project (3 pre-existing projects had derives already).
PROJECTS=$(docker exec georag-postgresql psql -U georag -d georag -tA -c "
    SELECT project_id::text
      FROM silver.projects
     WHERE workspace_id = '${WS_ID}'
       AND slug LIKE 'wsgs-uranium-%'
     ORDER BY slug;")

count=0
ok=0
fail=0
empty=0
while read -r PID; do
    [ -z "$PID" ] && continue
    count=$((count+1))
    out=$(docker exec georag-fastapi python3 -m app.services.ingest.derive_intervals --project-id "$PID" 2>&1 | tail -3)
    rc=$?
    if [ $rc -eq 0 ]; then
        if echo "$out" | grep -q "collars_processed.*0"; then
            empty=$((empty+1))
            echo "  [$count] $PID empty" >> "$OUTLOG"
        else
            ok=$((ok+1))
            echo "  [$count] $PID ok: $out" >> "$OUTLOG"
        fi
    else
        fail=$((fail+1))
        echo "  [$count] $PID FAIL: $out" >> "$OUTLOG"
    fi
done <<< "$PROJECTS"

echo "==> Done: total=$count ok=$ok empty=$empty fail=$fail" | tee -a "$OUTLOG"
