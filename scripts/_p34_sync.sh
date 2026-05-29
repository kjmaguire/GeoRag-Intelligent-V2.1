#!/usr/bin/env bash
set -u
SRC=/mnt/c/Users/GeoRAG/Herd/georag/src/fastapi/app/agent/prompts
DST=/home/georag/projects/georag/src/fastapi/app/agent/prompts
for f in orchestrator_default_dash.py orchestrator_numeric_dash.py orchestrator_narrative_dash.py orchestrator_graph_dash.py; do
    cp "$SRC/$f" "$DST/$f"
    sed -i 's/\r$//' "$DST/$f"
done
echo "synced 4 prompt modules"
