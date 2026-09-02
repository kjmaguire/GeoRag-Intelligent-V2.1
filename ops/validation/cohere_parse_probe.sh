#!/usr/bin/env bash
# Run the Cohere Parse wire-contract probe with the FastAPI service's venv.
#
# Needs AZURE_FOUNDRY_ENDPOINT / AZURE_FOUNDRY_API_KEY (and optionally
# AZURE_FOUNDRY_PARSE_DEPLOYMENT, default Cohere-parse-v5) in the environment.
# Writes ops/validation/reports/cohere_parse_probe_<timestamp>.json.
#
# Usage:
#   bash ops/validation/cohere_parse_probe.sh                       # fixture PDF
#   PROBE_PDF=/path/to/scan.pdf PROBE_PAGES=1,3 bash ops/validation/cohere_parse_probe.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

PDF="${PROBE_PDF:-src/fastapi/tests/fixtures/ocr/PLS-2024-Technical-Report.pdf}"
PAGES="${PROBE_PAGES:-1}"
TABLE_PAGE="${PROBE_TABLE_PAGE:-}"

args=(--pdf "$PDF" --pages "$PAGES")
[ -n "$TABLE_PAGE" ] && args+=(--table-page "$TABLE_PAGE")

(cd src/fastapi && uv run --no-sync python "$REPO_ROOT/ops/validation/cohere_parse_probe.py" "${args[@]}")
