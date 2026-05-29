#!/usr/bin/env bash
set -u
SRC=/mnt/c/Users/GeoRAG/Herd/georag/src/fastapi/app/agent/prompts
DST=/home/georag/projects/georag/src/fastapi/app/agent/prompts
for f in orchestrator_shared_preamble_colon.py orchestrator_default_colon.py orchestrator_numeric_colon.py orchestrator_narrative_colon.py orchestrator_graph_colon.py; do
    cp "$SRC/$f" "$DST/$f"
    sed -i 's/\r$//' "$DST/$f"
done
echo "synced 5 colon prompt modules"
