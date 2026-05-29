#!/usr/bin/env bash
# Master-plan §3 Step 9 acceptance test — 50-PDF corpus validator.
#
# Thin wrapper that runs phase3_master_plan_acceptance.py inside
# georag-fastapi. The Python script does the actual work.
#
# Usage:
#   bash scripts/phase3_master_plan_acceptance.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

CONTAINER="${CONTAINER:-georag-fastapi}"

# Copy the script + corpus dir into the container fresh on each run.
# The corpus dir at $REPO_ROOT/tests/fixtures/phase3_pdf_corpus lives
# outside the container's /app bind mount, so we shuttle it in via
# docker cp.
docker cp "$SCRIPT_DIR/phase3_master_plan_acceptance.py" \
    "$CONTAINER:/tmp/phase3_master_plan_acceptance.py"

# Clean prior corpus copy + ship fresh
docker exec "$CONTAINER" rm -rf /tmp/phase3_pdf_corpus 2>/dev/null || true
if [ -d "$REPO_ROOT/tests/fixtures/phase3_pdf_corpus" ]; then
    docker cp "$REPO_ROOT/tests/fixtures/phase3_pdf_corpus" \
        "$CONTAINER:/tmp/phase3_pdf_corpus"
fi

docker exec "$CONTAINER" python /tmp/phase3_master_plan_acceptance.py
