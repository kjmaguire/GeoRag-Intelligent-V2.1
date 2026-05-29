#!/bin/bash
# ADR-0011 dependency-chain driver — runs the prerequisites for the next
# reranker training cycle end-to-end without any manual gates.
#
# Steps:
#   1. Verify the Earle textbook project + workspace already exist in
#      silver.projects (created by hand earlier via SQL INSERT).
#   2. Stage chapter PDFs into the fastapi container's persistent volume.
#   3. Run scripts/_ingest_earle_textbook.py — uploads to bronze, fires
#      ingest_pdf for each, post-updates OER metadata on silver.reports.
#   4. Wait for the embed sweep to catch up (silver.document_passages
#      should have populated chunks for all 17 chapters).
#   5. Run scripts/_extract_domain_vocab.py — TSV of high-recurrence
#      OOV domain terms (includes the just-ingested textbook content).
#   6. Run scripts/_extend_reranker_tokenizer.py — expanded tokenizer +
#      resized + mean-of-subword-initialized embeddings, saved to
#      /tmp/hf_cache/_bge_extended/v1-<date>/ inside fastapi.
#
# After this completes, the next manual GPU-window steps are:
#   7. Pause vLLM + hatchet-worker-ai
#   8. Run scripts/_train_mlm_continued.py
#   9. Run scripts/_train_reranker_full.py
#  10. Restart paused services + eval + bench
#
# Locked constants:
ABS_WORKSPACE_ID="a0000000-0000-0000-0000-000000000001"
ABS_PROJECT_ID="db8ae12a-0767-441d-9171-065c5f501dde"
BRONZE_DIR_HOST="bronze/textbooks/earle_physical_geology"
BRONZE_DIR_CONTAINER="/tmp/hf_cache/_textbooks/earle"
VOCAB_TSV_CONTAINER="/tmp/hf_cache/_vocab/candidates.tsv"
EXTENDED_DIR_CONTAINER="/tmp/hf_cache/_bge_extended/v1-$(date +%Y%m%d)"

set -euo pipefail

echo "=== ADR-0011 dependency-chain driver ==="
echo "workspace_id=$ABS_WORKSPACE_ID"
echo "project_id=$ABS_PROJECT_ID"

# Step 1 — workspace + project sanity
echo ""
echo "Step 1: verify workspace + project ..."
docker exec georag-postgresql psql -U georag -d georag -t -c "
SELECT
    'workspace: ' || w.name || ' (' || w.workspace_id::text || ')' AS sanity_check_1,
    'project: ' || p.project_name || ' (' || p.project_id::text || ')' AS sanity_check_2
FROM silver.workspaces w
JOIN silver.projects p ON p.workspace_id = w.workspace_id
WHERE w.workspace_id = '$ABS_WORKSPACE_ID'::uuid
  AND p.project_id = '$ABS_PROJECT_ID'::uuid;
"

# Step 2 — stage chapter PDFs (idempotent)
echo ""
echo "Step 2: stage chapter PDFs to container persistent volume ..."
docker exec georag-fastapi bash -c "mkdir -p $BRONZE_DIR_CONTAINER && rm -f $BRONZE_DIR_CONTAINER/*"
docker cp "$BRONZE_DIR_HOST/." "georag-fastapi:$BRONZE_DIR_CONTAINER/"
docker exec georag-fastapi bash -c "ls $BRONZE_DIR_CONTAINER/ | wc -l"

# Step 3 — fire the ingest
echo ""
echo "Step 3: ingest 17 chapter PDFs through ingest_pdf workflow ..."
docker cp scripts/_ingest_earle_textbook.py georag-fastapi:/tmp/_ingest_earle_textbook.py
docker exec georag-fastapi bash -c "
export LOG_LEVEL=INFO \
       WORKSPACE_ID=$ABS_WORKSPACE_ID \
       PROJECT_ID=$ABS_PROJECT_ID \
       BRONZE_DIR=$BRONZE_DIR_CONTAINER \
       FASTAPI_BASE=http://localhost:8000
python /tmp/_ingest_earle_textbook.py 2>&1
"

# Step 4 — wait for embed sweep
echo ""
echo "Step 4: wait for embed sweep to populate document_passages ..."
EXPECTED_REPORTS=17
for attempt in $(seq 1 60); do
    PASSAGE_COUNT=$(docker exec georag-postgresql psql -U georag -d georag -t -A -c "
        SELECT COUNT(*) FROM silver.document_passages p
        JOIN silver.reports r ON r.report_id = p.document_id
        WHERE r.workspace_id = '$ABS_WORKSPACE_ID'::uuid
          AND r.project_id = '$ABS_PROJECT_ID'::uuid
    ")
    REPORT_COUNT=$(docker exec georag-postgresql psql -U georag -d georag -t -A -c "
        SELECT COUNT(*) FROM silver.reports
        WHERE workspace_id = '$ABS_WORKSPACE_ID'::uuid
          AND project_id = '$ABS_PROJECT_ID'::uuid
    ")
    echo "  attempt $attempt: reports=$REPORT_COUNT passages=$PASSAGE_COUNT"
    if [ "$REPORT_COUNT" -ge $EXPECTED_REPORTS ] && [ "$PASSAGE_COUNT" -gt 100 ]; then
        echo "  ready."
        break
    fi
    sleep 30
done

# Step 5 — vocab extraction
echo ""
echo "Step 5: extract domain vocab candidates from full corpus ..."
docker cp scripts/_extract_domain_vocab.py georag-fastapi:/tmp/_extract_domain_vocab.py
docker exec georag-fastapi bash -c "
mkdir -p \$(dirname $VOCAB_TSV_CONTAINER)
export LOG_LEVEL=INFO
python /tmp/_extract_domain_vocab.py \
    --output $VOCAB_TSV_CONTAINER \
    --min-chunk-freq 100 \
    --min-subword-count 3 \
    --top-k 5000
"
docker exec georag-fastapi bash -c "wc -l $VOCAB_TSV_CONTAINER && head -10 $VOCAB_TSV_CONTAINER"

# Step 6 — tokenizer extension
echo ""
echo "Step 6: extend bge-reranker-base tokenizer ..."
docker cp scripts/_extend_reranker_tokenizer.py georag-fastapi:/tmp/_extend_reranker_tokenizer.py
docker exec georag-fastapi bash -c "
mkdir -p $EXTENDED_DIR_CONTAINER
export LOG_LEVEL=INFO
python /tmp/_extend_reranker_tokenizer.py \
    --vocab-tsv $VOCAB_TSV_CONTAINER \
    --output $EXTENDED_DIR_CONTAINER \
    --base-model BAAI/bge-reranker-base
"
docker exec georag-fastapi bash -c "ls -lh $EXTENDED_DIR_CONTAINER/"

echo ""
echo "=== chain complete ==="
echo "Vocab TSV:        $VOCAB_TSV_CONTAINER (inside georag-fastapi)"
echo "Expanded backbone: $EXTENDED_DIR_CONTAINER (inside georag-fastapi)"
echo ""
echo "Next manual GPU-window steps (per ADR-0011):"
echo "  docker stop georag-vllm georag-hatchet-worker-ai"
echo "  docker exec georag-fastapi bash -c \\"
echo "    'export LOG_LEVEL=INFO && python /app/scripts/_train_mlm_continued.py \\"
echo "        --backbone $EXTENDED_DIR_CONTAINER \\"
echo "        --epochs 2 --batch-size 16 --grad-accum 4 --learning-rate 5e-5'"
echo "  ... then _train_reranker_full.py against the MLM output ..."
