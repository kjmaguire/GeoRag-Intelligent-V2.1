#!/usr/bin/env bash
# Run the Cohere API wire-contract probe with the FastAPI service's venv.
#
# The sibling of bedrock_probe.sh, which still covers the half that stayed on
# AWS (Embed v4, Rerank 3.5). Chat and Parse moved to Cohere's own API on
# 2026-09-15 (ADR-0023). RUN BOTH — neither covers the other's models, and
# aws-preflight.sh A-11 wants a report from each.
#
# Needs COHERE_API_KEY, and a key whose plan covers BOTH command-a-plus and
# parse-v5.0. A key entitled to chat but not Parse deploys cleanly and then
# sends every scanned page to tesseract, which extracts no tables.
#
# Writes ops/validation/reports/cohere_probe_<timestamp>.json. COMMIT THE
# REPORT: both adapters carry [UNVERIFIED] at the top until one exists. The
# key itself is never written to the report — only its length.
#
# Usage:
#   COHERE_API_KEY=... bash ops/validation/cohere_probe.sh            # fixture PDF
#   COHERE_API_KEY=... PROBE_PDF=/path/to/scan.pdf PROBE_PAGES=1,3 \
#     bash ops/validation/cohere_probe.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

if [ -z "${COHERE_API_KEY:-}" ]; then
  echo "COHERE_API_KEY is unset — every section would report 'skipped' and the" >&2
  echo "run would verify nothing. Export it and re-run." >&2
  exit 2
fi

PDF="${PROBE_PDF:-src/fastapi/tests/fixtures/ocr/PLS-2024-Technical-Report.pdf}"
PAGES="${PROBE_PAGES:-1}"

(cd src/fastapi && uv run --no-sync python "$REPO_ROOT/ops/validation/cohere_probe.py" \
    --pdf "$REPO_ROOT/$PDF" --pages "$PAGES" \
    --out "$REPO_ROOT/ops/validation/reports")
