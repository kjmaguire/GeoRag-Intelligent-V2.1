#!/usr/bin/env bash
# Import-boundary lint for master-plan §3 Step 1 (doc-phase 49).
#
# Per ADR-0002: nothing under src/fastapi/app/routers/ or
# src/fastapi/app/main.py may import app.ocr. Only the Hatchet
# ingest_pdf workflow (and tests) may. This keeps PaddleOCR + Docling
# out of the user-facing FastAPI process's resident memory.
#
# Allowed importers:
#   - src/fastapi/app/hatchet_workflows/ingest_pdf.py
#   - src/fastapi/app/ocr/  (internal cross-module imports)
#   - src/fastapi/tests/  (any test file)
#
# Exits 0 on clean lint; 1 on violation.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

PATTERN='(import |from )app\.ocr'
SEARCH_ROOT="src/fastapi"

# Allow-list:
# - app/ocr/ — internal cross-module imports
# - app/hatchet_workflows/ — ALL Hatchet workflows OK; they run in the
#   hatchet-worker-ingestion container which is the intended host for
#   the heavy OCR stack (PaddleOCR/Docling). Doc-phase 63 added
#   re_ocr_page.py here.
# - app/routers/ocr_render.py — doc-phase 59; imports only app.ocr.render
#   (light, pypdfium2 only). Note: any router importing the heavy
#   parser modules would still pull them into the user-facing FastAPI
#   process — that intent of the rule is preserved by listing routers
#   one by one, not allow-listing app/routers/ broadly.
# - tests/ — anything under tests/
ALLOWED_PATTERN='^src/fastapi/(app/ocr/|app/hatchet_workflows/|app/routers/ocr_render\.py|tests/)'

violations=()

# Use ripgrep if available (faster, project standard); fall back to grep.
if command -v rg >/dev/null 2>&1; then
    MATCHES=$(rg --no-heading --line-number "$PATTERN" "$SEARCH_ROOT" || true)
else
    MATCHES=$(grep -rn -E "$PATTERN" "$SEARCH_ROOT" || true)
fi

if [ -z "$MATCHES" ]; then
    echo "[step1-import-boundary] no app.ocr importers found yet (expected for Step 1 skeleton)"
    echo "[step1-import-boundary] PASS"
    exit 0
fi

while IFS= read -r line; do
    # Format from rg/grep: <path>:<lineno>:<content>
    path=${line%%:*}
    if ! echo "$path" | grep -qE "$ALLOWED_PATTERN"; then
        violations+=("$line")
    fi
done <<< "$MATCHES"

if [ ${#violations[@]} -eq 0 ]; then
    echo "[step1-import-boundary] all app.ocr importers are inside allow-list"
    echo "[step1-import-boundary] PASS"
    exit 0
fi

echo "[step1-import-boundary] FAIL: ${#violations[@]} app.ocr import(s) outside the allow-list:" >&2
for v in "${violations[@]}"; do
    echo "  $v" >&2
done
echo "" >&2
echo "Allowed importers per ADR-0002:" >&2
echo "  - src/fastapi/app/hatchet_workflows/ingest_pdf.py" >&2
echo "  - src/fastapi/app/ocr/  (internal)" >&2
echo "  - src/fastapi/tests/" >&2
exit 1
