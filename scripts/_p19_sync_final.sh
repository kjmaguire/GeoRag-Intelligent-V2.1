#!/usr/bin/env bash
set -u
for f in phase19_step1_verify.sh phase19_step2_verify.sh phase19_step3_verify.sh phase19_step4_verify.sh phase19_master_sweep.sh; do
    cp "/mnt/c/Users/GeoRAG/Herd/georag/scripts/$f" "/home/georag/projects/georag/scripts/$f"
    sed -i 's/\r$//' "/home/georag/projects/georag/scripts/$f"
    chmod +x "/home/georag/projects/georag/scripts/$f"
done
for d in phase19_implementation_kickoff.md phase19_golden_baseline_v4.md phase19_handoff.md; do
    cp "/mnt/c/Users/GeoRAG/Herd/georag/docs/$d" "/home/georag/projects/georag/docs/$d"
    sed -i 's/\r$//' "/home/georag/projects/georag/docs/$d"
done
echo "synced"
