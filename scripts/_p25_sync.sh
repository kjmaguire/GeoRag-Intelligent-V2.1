#!/usr/bin/env bash
set -u
for f in phase25_step1_verify.sh phase25_master_sweep.sh; do
    cp "/mnt/c/Users/GeoRAG/Herd/georag/scripts/$f" "/home/georag/projects/georag/scripts/$f"
    sed -i 's/\r$//' "/home/georag/projects/georag/scripts/$f"
    chmod +x "/home/georag/projects/georag/scripts/$f"
done
for d in phase25_handoff.md; do
    cp "/mnt/c/Users/GeoRAG/Herd/georag/docs/$d" "/home/georag/projects/georag/docs/$d"
    sed -i 's/\r$//' "/home/georag/projects/georag/docs/$d"
done
echo "synced"
