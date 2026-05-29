#!/usr/bin/env bash
set -u
for f in phase18_step4_verify.sh phase18_master_sweep.sh; do
    cp "/mnt/c/Users/GeoRAG/Herd/georag/scripts/$f" "/home/georag/projects/georag/scripts/$f"
    sed -i 's/\r$//' "/home/georag/projects/georag/scripts/$f"
    chmod +x "/home/georag/projects/georag/scripts/$f"
done
for d in phase18_golden_baseline_v3.md phase18_handoff.md; do
    cp "/mnt/c/Users/GeoRAG/Herd/georag/docs/$d" "/home/georag/projects/georag/docs/$d"
    sed -i 's/\r$//' "/home/georag/projects/georag/docs/$d"
done
echo "synced"
