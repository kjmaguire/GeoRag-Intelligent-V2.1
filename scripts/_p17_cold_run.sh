#!/usr/bin/env bash
# Restart fastapi to clear in-process caches, then run the golden suite once.
set -uo pipefail
cd /home/georag/projects/georag
docker compose restart fastapi >/dev/null 2>&1 || true
for _ in $(seq 1 30); do
    s=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/health 2>/dev/null || true)
    [ "$s" = "200" ] && break
    sleep 2
done
echo "health=$s"
docker exec georag-fastapi pytest --tb=no -q /app/tests/test_golden_queries.py 2>&1 | tail -2
