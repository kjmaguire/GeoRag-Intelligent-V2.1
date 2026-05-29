#!/usr/bin/env bash
ORCH=/home/georag/projects/georag/src/fastapi/app/agent/orchestrator.py
echo "1. query_downhole_logs comma:"
grep -c '"query_downhole_logs",' "$ORCH"
echo "2. DownholeLogsResult( open:"
grep -c 'DownholeLogsResult($' "$ORCH"
echo "3. _dh_collar:"
grep -c '_dh_collar' "$ORCH"
echo "4. LithologyInterval import:"
grep -cE '^[[:space:]]+LithologyInterval,$' "$ORCH"
echo "5. Direct file head:"
head -55 "$ORCH" | tail -10
