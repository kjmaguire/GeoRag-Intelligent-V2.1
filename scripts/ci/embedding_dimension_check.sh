#!/usr/bin/env bash
# =============================================================================
# scripts/ci/embedding_dimension_check.sh
#
# Guards the 384-vs-1024 embedding dimension landmine (Phase A item A6).
#
# The 2026-06-03 model swap moved the corpus from BAAI/bge-small-en-v1.5
# (384-dim) to Qwen/Qwen3-Embedding-0.6B (1024-dim) and recreated the
# georag_chunks Qdrant collection at 1024. The live .env was updated; the
# compose defaults were not. Three services carried
# `${EMBEDDING_MODEL_NAME:-BAAI/bge-small-en-v1.5}`:
#
#   fastapi          — embeds the query
#   embedding        — the shared CPU model sidecar both others proxy to
#   hatchet-worker   — embeds passages at ingest time
#
# Any deploy that lost .env — the normal condition for a fresh Azure
# Container Apps revision — would load a 384-dim model and write 384-dim
# vectors into a 1024-dim collection. Qdrant answers with an opaque HTTP 400
# and the user sees a bare refusal with no stated cause.
#
# This lives in a shell gate rather than pytest because the FastAPI test
# container mounts only src/fastapi as /app, so docker-compose.yml is not
# reachable from a test run. The code-side half of the guard (app/config.py
# vs app/embedding_service.py, plus the qdrant_dense_dim reader) IS in
# pytest: src/fastapi/tests/test_embedding_dimension_parity.py
#
# Exit 0 = clean. Exit 1 = a default disagrees with the corpus model.
# =============================================================================

set -uo pipefail

HERE="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$HERE"

# The corpus model and its dense width. Change BOTH together, and only
# alongside a deliberate re-embed (scripts/reembed_qdrant.py).
EXPECTED_MODEL="Qwen/Qwen3-Embedding-0.6B"
EXPECTED_DIM="1024"

# Models whose dense width is NOT ${EXPECTED_DIM}. A default naming any of
# these is always a bug, whatever the current corpus model happens to be.
WRONG_DIM_MODELS=(
    "BAAI/bge-small-en-v1.5"                        # 384
    "sentence-transformers/all-MiniLM-L6-v2"        # 384
    "BAAI/bge-base-en-v1.5"                         # 768
)

COMPOSE_FILES=(docker-compose.yml)
[[ -f docker-compose.demo.yml ]] && COMPOSE_FILES+=(docker-compose.demo.yml)

status=0

for f in "${COMPOSE_FILES[@]}"; do
    # ---- model-name defaults -------------------------------------------------
    # Matches `EMBEDDING_MODEL_NAME: ${EMBEDDING_MODEL_NAME:-<default>}` and
    # captures <default>.
    mapfile -t defaults < <(
        grep -oE 'EMBEDDING_MODEL_NAME:[[:space:]]*\$\{EMBEDDING_MODEL_NAME:-[^}]+\}' "$f" \
            | sed -E 's/.*:-([^}]+)\}/\1/'
    )

    for d in "${defaults[@]}"; do
        for bad in "${WRONG_DIM_MODELS[@]}"; do
            if [[ "$d" == "$bad" ]]; then
                echo "FAIL $f: EMBEDDING_MODEL_NAME defaults to '$d', whose dense" >&2
                echo "     width is not ${EXPECTED_DIM}. A deploy without .env would write" >&2
                echo "     wrong-width vectors into georag_chunks and every search" >&2
                echo "     would 400. Expected default: ${EXPECTED_MODEL}" >&2
                status=1
            fi
        done
        if [[ "$d" != "$EXPECTED_MODEL" ]]; then
            echo "FAIL $f: EMBEDDING_MODEL_NAME default '$d' != '${EXPECTED_MODEL}'." >&2
            echo "     All services must agree — the worker writes the vectors the" >&2
            echo "     fastapi query path reads." >&2
            status=1
        fi
    done

    # ---- dimension defaults -------------------------------------------------
    mapfile -t dims < <(
        grep -oE 'EMBEDDING_DIMENSION:[[:space:]]*\$\{EMBEDDING_DIMENSION:-[0-9]+\}' "$f" \
            | sed -E 's/.*:-([0-9]+)\}/\1/'
    )

    for dim in "${dims[@]}"; do
        if [[ "$dim" != "$EXPECTED_DIM" ]]; then
            echo "FAIL $f: EMBEDDING_DIMENSION default '$dim' != '${EXPECTED_DIM}'" >&2
            echo "     (model ${EXPECTED_MODEL})." >&2
            status=1
        fi
    done
done

# A bare `EMBEDDING_MODEL_NAME: BAAI/...` with no ${...} indirection would slip
# past the pattern above, so flag any wrong-width model named anywhere in the
# compose env blocks.
for bad in "${WRONG_DIM_MODELS[@]}"; do
    if grep -nE "EMBEDDING_MODEL_NAME:[[:space:]]*${bad//\//\\/}" "${COMPOSE_FILES[@]}" 2>/dev/null; then
        echo "FAIL: hard-coded wrong-width embedding model above." >&2
        status=1
    fi
done

if [[ $status -eq 0 ]]; then
    echo "embedding_dimension_check: clean (${EXPECTED_MODEL}, ${EXPECTED_DIM}-dim)"
fi

exit $status
