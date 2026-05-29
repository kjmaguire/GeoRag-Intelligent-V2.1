#!/usr/bin/env bash
set -u
for f in phase18_step5_verify.sh phase18_master_sweep.sh; do
    cp "/mnt/c/Users/GeoRAG/Herd/georag/scripts/$f" "/home/georag/projects/georag/scripts/$f"
    sed -i 's/\r$//' "/home/georag/projects/georag/scripts/$f"
    chmod +x "/home/georag/projects/georag/scripts/$f"
done
echo "synced"
