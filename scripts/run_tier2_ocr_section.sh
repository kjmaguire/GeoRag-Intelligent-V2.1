#!/usr/bin/env bash
# =============================================================================
# scripts/run_tier2_ocr_section.sh
#
# Tier 2 OCR pipeline runner for a single TRS section (Township-Range-Section).
# Extracts the section from Uranium_Logs_ALL.zip into the phase-b-extract
# volume, runs ocr_cluster_tiffs against every TIFF in that section, and
# cleans up the extraction.
#
# Usage:
#   bash scripts/run_tier2_ocr_section.sh <SECTION_TRS> [MAX_FILES]
#
# Examples:
#   bash scripts/run_tier2_ocr_section.sh 024N093W10           # full section
#   bash scripts/run_tier2_ocr_section.sh 028N079W36 50        # cap at 50 TIFFs
#
# Cost: ~30-60s/page on CPU Tesseract — a full section can take many hours.
# Use MAX_FILES for smoke testing.
# =============================================================================

set -uo pipefail

SECTION="${1:?usage: $0 <SECTION_TRS> [MAX_FILES]}"
MAX_FILES="${2:-}"
WS_ID="${WS_ID:-a0000000-0000-0000-0000-000000000001}"
ZIP_PATH="${ZIP_PATH:-/c/Users/GeoRAG/Desktop/Uranium_Logs_ALL.zip}"
EXTRACT_BASE="${EXTRACT_BASE:-/var/lib/docker/volumes/georag-phase-b-extract/_data}"
OUTLOG="docs/tier2_ocr_${SECTION}.log"

echo "==> Tier 2 OCR for $SECTION starts $(date -u +%FT%TZ)" | tee "$OUTLOG"

# Look up the project_id for the section's slug.
PID=$(docker exec georag-postgresql psql -U georag -d georag -tA -c "
    SELECT project_id::text FROM silver.projects
     WHERE workspace_id = '${WS_ID}'
       AND slug LIKE '%${SECTION,,}%'
     LIMIT 1;")

if [ -z "$PID" ]; then
    echo "ERROR: no silver.projects row for section $SECTION" | tee -a "$OUTLOG"
    exit 1
fi
echo "  project_id=$PID" | tee -a "$OUTLOG"

# Extract the section's inner zip into the phase-b-extract volume.
echo "==> Extracting $SECTION.zip from archive..." | tee -a "$OUTLOG"
python3 -c "
import zipfile, io, os, sys
zf = zipfile.ZipFile(r'$ZIP_PATH', 'r')
section_path = f'uranium-logs_TRS/${SECTION}.zip'
try:
    inner_bytes = zf.read(section_path)
except KeyError:
    print(f'section not in archive: {section_path}')
    sys.exit(1)
out_dir = r'$EXTRACT_BASE' + '/${SECTION}'
os.makedirs(out_dir, exist_ok=True)
inner = zipfile.ZipFile(io.BytesIO(inner_bytes), 'r')
n = 0
for member in inner.namelist():
    if member.lower().endswith(('.tif', '.tiff')):
        inner.extract(member, out_dir)
        n += 1
        if n % 100 == 0:
            print(f'  extracted {n} tiffs')
print(f'  total extracted: {n} tiffs')
"

MAX_ARG=""
[ -n "$MAX_FILES" ] && MAX_ARG="max_files=${MAX_FILES}, "

# Run OCR
echo "==> Running ocr_cluster_tiffs..." | tee -a "$OUTLOG"
MSYS_NO_PATHCONV=1 docker exec georag-fastapi python3 -c "
import asyncio, sys, os
sys.path.insert(0, '/app')
from app.services.ingest.tiff_ocr_ingester import ocr_cluster_tiffs

async def main():
    r = await ocr_cluster_tiffs(
        '/data/${SECTION}',
        workspace_id='${WS_ID}',
        project_id='${PID}',
        ${MAX_ARG}
    )
    print('==== OCR RESULT ====')
    print(r)

asyncio.run(main())
" 2>&1 | tee -a "$OUTLOG"

# Cleanup
echo "==> Cleanup ${EXTRACT_BASE}/${SECTION}" | tee -a "$OUTLOG"
rm -rf "${EXTRACT_BASE}/${SECTION}" 2>/dev/null

echo "==> Tier 2 OCR for $SECTION complete: $(date -u +%FT%TZ)" | tee -a "$OUTLOG"
