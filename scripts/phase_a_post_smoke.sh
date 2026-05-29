#!/usr/bin/env bash
# Phase A post-walk smoke test runner.
#
# Run this AFTER `inspect_ingest_zip.py` has completed against the
# Uranium archive. Surveys the bronze.ingest_manifest, samples
# clusters, and runs the eval pipeline as a sanity check that the
# rest of the platform is still operational after a 200GB ingest pass.
#
# Doc-phase 178 / Tier 3 of overnight autonomous mandate.

set -e

POSTGRES="docker exec georag-postgresql psql -U georag -d georag"
FASTAPI="georag-fastapi"

echo "=========================================="
echo "Phase A post-walk smoke test"
echo "=========================================="
echo ""

echo "--- 1. Most recent ingest_run summary ---"
$POSTGRES -c "
SELECT
    run_id,
    source_path,
    status,
    started_at,
    completed_at,
    completed_at - started_at AS duration,
    files_seen,
    files_indexed,
    pg_size_pretty(bytes_seen) AS bytes_seen,
    summary_payload->'cluster_count' AS clusters,
    summary_payload->'type_counts' AS type_counts
  FROM bronze.ingest_runs
 ORDER BY started_at DESC
 LIMIT 1;
"

LATEST_RUN_ID=$($POSTGRES -t -c "SELECT run_id FROM bronze.ingest_runs ORDER BY started_at DESC LIMIT 1;" | xargs)

if [ -z "$LATEST_RUN_ID" ]; then
    echo "No ingest_runs found; skipping per-run checks."
else
    echo ""
    echo "--- 2. Top 20 clusters by file count (run: $LATEST_RUN_ID) ---"
    $POSTGRES -c "
    SELECT
        cluster_key,
        guessed_project,
        count(*) AS file_count,
        pg_size_pretty(sum(file_size_bytes)::bigint) AS total_size,
        round(avg(tiff_width)::numeric, 0) AS avg_w,
        round(avg(tiff_height)::numeric, 0) AS avg_h,
        sum(tiff_pages) AS total_pages
      FROM bronze.ingest_manifest
     WHERE run_id = '$LATEST_RUN_ID'::uuid
     GROUP BY cluster_key, guessed_project
     ORDER BY count(*) DESC
     LIMIT 20;
    "

    echo ""
    echo "--- 3. File type distribution ---"
    $POSTGRES -c "
    SELECT file_type, count(*) AS n,
           pg_size_pretty(sum(file_size_bytes)::bigint) AS total_size
      FROM bronze.ingest_manifest
     WHERE run_id = '$LATEST_RUN_ID'::uuid
     GROUP BY file_type
     ORDER BY count(*) DESC;
    "

    echo ""
    echo "--- 4. Anomalies sample (first 10) ---"
    $POSTGRES -c "
    SELECT file_path_in_zip, anomalies
      FROM bronze.ingest_manifest
     WHERE run_id = '$LATEST_RUN_ID'::uuid
       AND jsonb_array_length(anomalies) > 0
     LIMIT 10;
    "

    echo ""
    echo "--- 5. TIFF metadata distribution (5th, 50th, 95th percentile) ---"
    $POSTGRES -c "
    SELECT
        percentile_disc(0.05) WITHIN GROUP (ORDER BY tiff_width) AS p5_width,
        percentile_disc(0.50) WITHIN GROUP (ORDER BY tiff_width) AS p50_width,
        percentile_disc(0.95) WITHIN GROUP (ORDER BY tiff_width) AS p95_width,
        percentile_disc(0.05) WITHIN GROUP (ORDER BY tiff_height) AS p5_height,
        percentile_disc(0.50) WITHIN GROUP (ORDER BY tiff_height) AS p50_height,
        percentile_disc(0.95) WITHIN GROUP (ORDER BY tiff_height) AS p95_height,
        percentile_disc(0.50) WITHIN GROUP (ORDER BY tiff_pages) AS p50_pages,
        max(tiff_pages) AS max_pages,
        count(DISTINCT tiff_compression) AS distinct_compressions
      FROM bronze.ingest_manifest
     WHERE run_id = '$LATEST_RUN_ID'::uuid
       AND file_type = 'tiff';
    "
fi

echo ""
echo "--- 6. Substrate verifier ---"
cd /home/georag/projects/georag
bash scripts/autonomous_run_substrate_verify.sh 2>&1 | tail -3

echo ""
echo "--- 7. FastAPI eval regression ---"
docker exec $FASTAPI python -m pytest \
    tests/test_eval_validators.py \
    tests/test_real_rag_evaluator.py \
    tests/test_evaluate_workspace_workflow.py \
    tests/test_eval_real_rag_nightly_workflow.py \
    2>&1 | tail -3

echo ""
echo "--- 8. Hatchet workflow registration ---"
docker exec $FASTAPI python -m app.hatchet_workflows.worker --list 2>&1 | head -15

echo ""
echo "--- 9. Top-level service health ==="
docker ps --format 'table {{.Names}}\t{{.Status}}' 2>&1 | grep -E 'georag-|NAMES' | head -30

echo ""
echo "=========================================="
echo "Phase A post-walk smoke test COMPLETE"
echo "=========================================="
