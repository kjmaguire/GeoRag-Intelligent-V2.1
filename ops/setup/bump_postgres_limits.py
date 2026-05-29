#!/usr/bin/env python3
"""One-shot: bump postgresql container resource limits in docker-compose.yml.

For the v1.14 Threadripper hardware refresh:
  * cpus 4.0 -> 8.0  (16-core box, single-purpose dev)
  * memory 12G -> 16G (shared_buffers=8GB + work_mem*concurrency + maintenance_work_mem=2GB)
  * reservations 6G -> 10G
  * shm_size 1gb (lift temp-file ceiling for max_parallel_workers=12)

Idempotent — re-running on already-bumped values leaves them alone.
"""
import pathlib
import re
import sys

P = pathlib.Path("/home/georag/projects/georag/docker-compose.yml")
text = P.read_text()

m = re.search(r"(?ms)^  postgresql:\n.*?(?=^  [a-z][a-z0-9_-]*:\n)", text)
if not m:
    sys.exit("postgresql block not found")
block = m.group(0)
new_block = block

# Bump cpus
new_block = re.sub(r'(\s+cpus: ")4\.0(")', r'\g<1>8.0\g<2>', new_block, count=1)
# Bump memory limit
new_block = re.sub(r'(\s+memory: )12G\b', r'\g<1>16G', new_block, count=1)
# Bump memory reservation
new_block = re.sub(r'(\s+memory: )6G\b', r'\g<1>10G', new_block, count=1)

# Add shm_size after the deploy:resources block (idempotent)
if "shm_size:" not in new_block:
    pattern = re.compile(
        r'(    deploy:\n      resources:\n        limits:\n          cpus: "8\.0"\n          memory: 16G\n        reservations:\n          memory: 10G\n)'
    )
    new_block = pattern.sub(r'\1    shm_size: 1gb\n', new_block, count=1)

if new_block == block:
    print("No changes (already at target values).")
else:
    text = text.replace(block, new_block, 1)
    P.write_text(text)
    print("Postgres limits updated.")
