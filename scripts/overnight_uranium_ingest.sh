#!/usr/bin/env bash
# =============================================================================
# scripts/overnight_uranium_ingest.sh
#
# Overnight orchestrator: ingest the 98 priority PLSS-section bundles from
# Uranium_Logs_ALL.zip into georag silver.* / gold.* tables.
#
# Approach (pipelined, per-section):
#   1. Extract one inner zip from outer Uranium_Logs_ALL.zip into staging.
#   2. Run ingest_one_cluster.py on the extracted dir (LAS + .log + PDF + XLSX).
#   3. Append the result to /tmp/uranium_ingest_progress.jsonl.
#   4. DELETE the extracted dir so disk usage stays bounded to one section.
#   5. Loop.
#
# Progress is resumable: on restart, sections that already appear in the
# progress log are skipped. The status file lives outside the staging
# volume so it survives volume wipes.
#
# Hold-back contract: 913 sections (~36 GB) are NEVER touched by this
# script. They live untouched inside the outer ZIP for Kyle's
# upload-feature regression test.
# =============================================================================

set -uo pipefail

# Python (Windows-native) needs Windows-style paths; bash uses /c/...
MANIFEST_WIN='C:/Users/GeoRAG/Herd/georag/docs/overnight_ingestion_manifest.json'
PROGRESS=/c/Users/GeoRAG/Herd/georag/docs/overnight_ingestion_progress.jsonl
SRC_ZIP_WIN='C:/Users/GeoRAG/Desktop/Uranium_Logs_ALL.zip'

WS_ID=${WS_ID:-a0000000-0000-0000-0000-000000000001}

echo "================================================================"
echo "OVERNIGHT URANIUM INGEST — Tier-1-yield-ranked"
echo "================================================================"
echo "  Manifest:    $MANIFEST_WIN"
echo "  Progress:    $PROGRESS"
echo "  Workspace:   $WS_ID"
echo "  Started:     $(date -u +%FT%TZ)"
echo

mkdir -p "$(dirname "$PROGRESS")"
touch "$PROGRESS"

# Read the section list using a Windows-style path that the Python on the
# host can open. (Git Bash mounts /c/... but host Python can't see that.)
SECTIONS=$(python3 -c "
import json
m = json.load(open(r'$MANIFEST_WIN'))
print(f'Loaded manifest: {m[\"tier1_set_count\"]} sections, {m[\"tier1_set_gb\"]} GB')
for s in m['sections']:
    print(s['section'] + '|' + s['path'])
" 2>&1)
# Drop the "Loaded manifest:" diagnostic line.
SECTION_LIST=$(echo "$SECTIONS" | grep -E '^[0-9A-Za-z_]+\|uranium-logs' )
echo "$SECTIONS" | head -1
SECTIONS="$SECTION_LIST"

total=$(echo "$SECTIONS" | wc -l)
done_count=0
fail_count=0
skip_count=0
i=0

while IFS='|' read -r SECTION ZIP_PATH; do
    i=$((i+1))
    [ -z "$SECTION" ] && continue

    # Resume guard — skip if already attempted.
    if grep -q "\"section\":\"$SECTION\"" "$PROGRESS" 2>/dev/null; then
        skip_count=$((skip_count+1))
        echo "[$i/$total] SKIP (already attempted): $SECTION"
        continue
    fi

    echo "[$i/$total] EXTRACT + INGEST: $SECTION"
    SEC_START=$(date +%s)

    # Step 1 — Extract the inner zip into the staging volume.
    # Use python3 + the Windows path. Extraction lands at
    # /data/<section>/ inside the alpine helper container.
    extract_err=""
    if ! python3 << PYEOF 2>/tmp/extract_err.log
import zipfile, os, sys, subprocess, shutil, tempfile
import os
src = r"C:/Users/GeoRAG/Desktop/Uranium_Logs_ALL.zip"
inner = "$ZIP_PATH"
section = "$SECTION"
# Extract the inner zip from the outer zip to a Windows temp dir.
tmpdir = tempfile.mkdtemp(prefix=f"uranium_{section}_")
inner_path = os.path.join(tmpdir, os.path.basename(inner))
with zipfile.ZipFile(src) as outer:
    with outer.open(inner) as r, open(inner_path, "wb") as w:
        shutil.copyfileobj(r, w)
# Now extract the inner zip into a section-named subdir.
section_dir = os.path.join(tmpdir, section)
with zipfile.ZipFile(inner_path) as inner_zf:
    inner_zf.extractall(section_dir)
# Copy section_dir → georag-phase-b-extract volume via docker cp helper.
# We launch a transient alpine container that mounts the volume, then
# docker cp from the host tmpdir into it.
subprocess.check_call([
    "docker", "run", "-d", "--rm",
    "--name", f"stage_helper_{section}",
    "-v", "georag-phase-b-extract:/data",
    "alpine", "sleep", "300",
])
try:
    # Wait for container readiness.
    import time; time.sleep(0.5)
    subprocess.check_call(["docker", "cp", section_dir, f"stage_helper_{section}:/data/"])
finally:
    subprocess.run(["docker", "rm", "-f", f"stage_helper_{section}"], check=False)
# Free the local tempdir immediately.
shutil.rmtree(tmpdir, ignore_errors=True)
print(f"extracted {section}")
PYEOF
    then
        extract_err=$(tail -3 /tmp/extract_err.log | tr '\n' ' ')
        fail_count=$((fail_count+1))
        echo "  [FAIL extract] $extract_err"
        printf '{"section":"%s","status":"extract_failed","error":%s,"ts":"%s"}\n' \
            "$SECTION" "$(echo "$extract_err" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')" \
            "$(date -u +%FT%TZ)" >> "$PROGRESS"
        continue
    fi

    EXTRACT_END=$(date +%s)
    extract_dur=$((EXTRACT_END - SEC_START))

    # Step 2 — Ingest via the existing cluster_runner inside fastapi.
    # Project metadata is derived from the section key. Cluster runner
    # creates one silver.projects row per (slug) — we use one-project-
    # per-section so different operators/companies get distinct entries.
    PROJECT_SLUG="wsgs-uranium-${SECTION,,}"
    PROJECT_NAME="WSGS Uranium ${SECTION}"
    COMPANY="WSGS Archive"
    REGION="Wyoming"
    # For Cameco Shirley Basin home section, use the canonical names.
    if [ "$SECTION" = "028N079W36" ]; then
        PROJECT_SLUG="cameco-shirley-basin"
        PROJECT_NAME="Cameco Shirley Basin Uranium"
        COMPANY="CAMECO RESOURCES"
        REGION="CARBON, WY"
    fi

    ingest_log=$(mktemp /tmp/ingest_${SECTION}_XXXX.log)
    # Guard against Git Bash MSYS auto-mangling `/data/...` → `C:/Program Files/Git/data/...`
    # when calling docker.exe — the staging volume is mounted at /data inside
    # the container; the path must travel unmodified.
    # Doubled-slash `//data/` defeats MSYS path-conv aliasing that
    # otherwise rewrites `/data/X` to `C:/Program Files/Git/data/X` when
    # the arg is passed to docker.exe (a Windows binary).
    if MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' docker exec -e WS_ID="$WS_ID" georag-fastapi python3 -m scripts.ingest_one_cluster \
        --cluster-dir "//data/${SECTION}" \
        --section-key "$SECTION" \
        --project-name "$PROJECT_NAME" \
        --project-slug "$PROJECT_SLUG" \
        --company "$COMPANY" \
        --region "$REGION" \
        --workspace-id "$WS_ID" > "$ingest_log" 2>&1; then
        INGEST_END=$(date +%s)
        ingest_dur=$((INGEST_END - EXTRACT_END))
        # Extract summary counts from the log.
        SUMMARY=$(grep -oE 'collars=[0-9]+|reports=[0-9]+|curves=[0-9]+|samples=[0-9]+|las=[0-9]+|log=[0-9]+|pdf=[0-9]+|xlsx=[0-9]+' "$ingest_log" | tr '\n' ' ')
        done_count=$((done_count+1))
        echo "  [PASS ingest] extract=${extract_dur}s ingest=${ingest_dur}s $SUMMARY"
        printf '{"section":"%s","status":"success","extract_s":%d,"ingest_s":%d,"summary":"%s","ts":"%s"}\n' \
            "$SECTION" "$extract_dur" "$ingest_dur" "$SUMMARY" \
            "$(date -u +%FT%TZ)" >> "$PROGRESS"
    else
        ingest_err=$(tail -5 "$ingest_log" | tr '\n' ' ' | head -c 500)
        fail_count=$((fail_count+1))
        echo "  [FAIL ingest] $ingest_err"
        printf '{"section":"%s","status":"ingest_failed","error":%s,"ts":"%s"}\n' \
            "$SECTION" "$(echo "$ingest_err" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')" \
            "$(date -u +%FT%TZ)" >> "$PROGRESS"
    fi
    rm -f "$ingest_log"

    # Step 3 — Free the extracted section to bound disk usage.
    docker run --rm -v georag-phase-b-extract:/data alpine sh -c "rm -rf /data/${SECTION}" 2>/dev/null

    echo "  → done=${done_count} fail=${fail_count} skip=${skip_count}"
    echo
done <<< "$SECTIONS"

echo "================================================================"
echo "OVERNIGHT URANIUM INGEST — COMPLETE"
echo "  Total sections:     $total"
echo "  Successful ingest:  $done_count"
echo "  Failed:             $fail_count"
echo "  Skipped (resume):   $skip_count"
echo "  Finished:           $(date -u +%FT%TZ)"
echo "================================================================"
