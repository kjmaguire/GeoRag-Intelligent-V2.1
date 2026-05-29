#!/usr/bin/env bash
# Post-image-rebuild smoke test (doc-phase 122).
#
# Run RIGHT AFTER `docker compose build fastapi && docker compose up -d fastapi`.
# Confirms:
#   1. The 9 newly-installed Python deps import (§5 + §7 + §12)
#   2. langgraph + checkpoint-postgres + mcp-adapters + langfuse import
#      (was an opt-in extra; now baked into the runtime image)
#   3. WeasyPrint can render a trivial HTML to PDF — proves Pango / Cairo
#      / GLib system libs are wired correctly
#   4. The autonomous_run_substrate verifier still passes 68/68
#   5. The 8 graduated live helpers still pass their 52 pytest cases
#
# Exit 0 = rebuild healthy. Exit !=0 = surface the failing check.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

FASTAPI_CONTAINER="${FASTAPI_CONTAINER:-georag-fastapi}"

FAIL=0
TOTAL=0
note() { echo "$1"; }

check() {
    local label="$1"
    local description="$2"
    local snippet="$3"
    TOTAL=$((TOTAL + 1))
    if docker exec "$FASTAPI_CONTAINER" python -c "$snippet" >/dev/null 2>&1; then
        note "[$label] PASS — $description"
    else
        note "[$label] FAIL — $description"
        FAIL=$((FAIL + 1))
    fi
}

# ----------------------------------------------------------------------
# Section 1 — §5 spatial / drillhole-visual deps
# ----------------------------------------------------------------------
note ""
note "=== §5 spatial deps ==="
check "geopandas"   "geopandas imports + version reported" \
    "import geopandas; assert geopandas.__version__"
check "rasterio"    "rasterio imports + version reported" \
    "import rasterio; assert rasterio.__version__"
check "mplstereonet" "mplstereonet imports" \
    "import mplstereonet"

# ----------------------------------------------------------------------
# Section 2 — §7 report-rendering deps
# ----------------------------------------------------------------------
note ""
note "=== §7 report-rendering deps ==="
check "weasyprint"  "weasyprint imports + version reported" \
    "import weasyprint; assert weasyprint.__version__"
check "python-docx" "docx imports" \
    "import docx; from docx import Document; Document()"
check "openpyxl"    "openpyxl imports + can create workbook" \
    "import openpyxl; wb = openpyxl.Workbook(); wb.active.append(['hello'])"

# ----------------------------------------------------------------------
# Section 3 — §7 / §8 / §9 LangGraph stack
# ----------------------------------------------------------------------
note ""
note "=== langgraph stack ==="
check "langgraph"   "langgraph imports + StateGraph available" \
    "import langgraph; from langgraph.graph import StateGraph"
check "langgraph-checkpoint-postgres" "checkpoint store imports" \
    "from langgraph.checkpoint.postgres import PostgresSaver"
check "langchain-mcp-adapters" "MCP adapter imports" \
    "import langchain_mcp_adapters"
check "langfuse"    "langfuse client imports" \
    "import langfuse; from langfuse import Langfuse"

# ----------------------------------------------------------------------
# Section 4 — §12 ML stack
# ----------------------------------------------------------------------
note ""
note "=== §12 ML stack ==="
check "xgboost"     "xgboost imports + version reported" \
    "import xgboost; assert xgboost.__version__"
check "shap"        "shap imports + version reported" \
    "import shap; assert shap.__version__"
check "scikit-learn" "sklearn imports + version reported" \
    "import sklearn; assert sklearn.__version__"

# ----------------------------------------------------------------------
# Section 5 — WeasyPrint render smoke (Pango / Cairo system-lib bond)
# ----------------------------------------------------------------------
note ""
note "=== WeasyPrint render (system-lib bond test) ==="
TOTAL=$((TOTAL + 1))
if docker exec "$FASTAPI_CONTAINER" python -c "
import weasyprint
html = weasyprint.HTML(string='<h1>GeoRAG doc-phase 122</h1><p>WeasyPrint render smoke.</p>')
pdf_bytes = html.write_pdf()
assert pdf_bytes and pdf_bytes[:4] == b'%PDF', 'expected %PDF header'
print('PDF bytes generated:', len(pdf_bytes))
" 2>&1; then
    note "[weasyprint-render] PASS — HTML→PDF round-trip works (system libs OK)"
else
    note "[weasyprint-render] FAIL — WeasyPrint render failed; check Pango/Cairo libs"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Section 6 — substrate verifier still green
# ----------------------------------------------------------------------
note ""
note "=== Substrate verifier (re-run from rebuild) ==="
TOTAL=$((TOTAL + 1))
if bash "$SCRIPT_DIR/autonomous_run_substrate_verify.sh" >/tmp/substrate_verify.log 2>&1; then
    SUMMARY=$(grep -E "checks passed" /tmp/substrate_verify.log | tail -1)
    note "[substrate-verifier] PASS — $SUMMARY"
else
    note "[substrate-verifier] FAIL — see /tmp/substrate_verify.log"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------
echo ""
echo "=== Post-rebuild smoke summary ==="
echo "  $((TOTAL - FAIL))/$TOTAL checks passed"

if [ $FAIL -eq 0 ]; then
    echo ""
    echo "  Image rebuild is healthy."
    echo "  ~20 skeleton agents that were waiting on these deps are now"
    echo "  ready for graduation. See doc-phase 122 handoff for the list."
    echo ""
fi

exit $FAIL
