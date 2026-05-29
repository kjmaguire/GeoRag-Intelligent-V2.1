# Volume Migration

**Module 10 Chunk 10.8** — consolidates carry-forwards from Module 1 C1
(volume-wipe cold-start test) and Module 9 9.7 (non-root container UID
migration). Owns the procedures for migrating Docker named volumes
between configurations cleanly.

## When you need this runbook

| Scenario | Procedure |
|----------|-----------|
| First-time apply of Module 9 9.7 on an existing dev env (root → non-root UID) | § Non-root UID migration |
| Cold-start test (Module 1 C1) — wipe volumes, verify recovery from backup | § Cold-start volume wipe |
| Resizing a volume (e.g. growing Loki retention) | § Volume resize |
| Moving to a different host | § Cross-host migration |
| Restoring after disk failure | § Disaster recovery |

## Reference: GeoRAG named volumes

| Volume | Service(s) | Critical? | Backup runbook |
|--------|------------|-----------|----------------|
| `postgres_data` | postgresql | YES | `backup-restore.md` |
| `pgbouncer_data` | pgbouncer | NO (stateless config) | recreate from compose |
| `redis_data` | redis | partial (queue state, sessions) | re-sync from MQ producers |
| `neo4j_data` | neo4j | YES | `neo4j-backup.md` |
| `qdrant_data` | qdrant | partial (re-derivable from PG) | `qdrant-snapshot.md` |
| `seaweedfs_data` | seaweedfs | YES (object store) | `s3-abstraction.md` |
| `seaweedfs_filer_data` | seaweedfs | YES | same |
| `ollama_models` | ollama | NO (re-pull from registry) | re-run `ollama pull` |
| `vllm_models` | vllm | NO | same |
| `fastapi_hf_cache` | fastapi | NO (re-downloads) | first query re-fetches |
| `dagster_home` | dagster-daemon, dagster-webserver | YES (run history) | `ingestion-pipeline.md` |
| `ragflow_data` | ragflow | YES if active | TBD |
| `backup_staging` | backup-agent | NO (transient) | recreated nightly |
| `prometheus_data` | prometheus | NO (7-day retention; tier metrics rebuild) | n/a |
| `grafana_data` | grafana | NO (dashboards in git) | re-provision |
| `alertmanager_data` | alertmanager | NO (silences are operator-set) | n/a |
| `loki_data` | loki | NO (30-day retention; logs rebuild) | n/a |
| `promtail_positions` | promtail | NO (resumes from start on miss) | n/a |

## Non-root UID migration (Module 9 9.7 carry-forward)

Existing dev environments have `fastapi_hf_cache`, `dagster_home`, and
similar volumes created with **root ownership** because the previous
compose ran as root. After pulling Module 9 9.7, the new non-root user
can't write to those volumes until they're re-chowned or recreated.

### Option A — recreate (faster, preserves no data)

For dev environments where state is regenerable.

```bash
docker compose stop fastapi dagster-daemon dagster-webserver
docker volume rm \
    georag_fastapi_hf_cache \
    georag_dagster_home
docker compose up -d fastapi dagster-daemon dagster-webserver
```

- FastAPI: re-downloads HF embedding models on first query (~3 min cold-start).
- Dagster: re-runs pending sensors on first boot.

### Option B — chown in place (preserves data)

For staging or production with operational state worth keeping.

```bash
# 1. Stop the affected services.
docker compose stop fastapi dagster-daemon dagster-webserver

# 2. chown each volume via a one-shot privileged container.
docker run --rm -v georag_fastapi_hf_cache:/data alpine \
    chown -R 33:33 /data           # www-data (FastAPI Dockerfile USER)

docker run --rm -v georag_dagster_home:/data alpine \
    chown -R 65534:65534 /data     # nobody (Dagster Dockerfile USER)

# 3. Restart.
docker compose up -d fastapi dagster-daemon dagster-webserver

# 4. Verify writes work:
docker compose exec fastapi sh -c 'touch /tmp/hf_cache/.testwrite && rm /tmp/hf_cache/.testwrite && echo OK'
docker compose exec dagster-daemon sh -c 'touch /opt/dagster/dagster_home/.testwrite && rm /opt/dagster/dagster_home/.testwrite && echo OK'
```

Both should print `OK`. Failure means the chown didn't take — verify
the UID matches the Dockerfile USER.

## Cold-start volume wipe (Module 1 C1)

The Module 1 C1 test verifies the platform recovers cleanly from a
catastrophic state loss. This is the closest GeoRAG comes to a DR drill
in V1.

```bash
# 1. CHECKPOINT — snapshot the most recent backup before wiping.
ls -la /var/lib/docker/volumes/georag_backup_staging/_data/
# Confirm a recent backup exists; copy off-host if you don't trust local.

# 2. Stop everything.
docker compose down

# 3. Remove ALL stateful volumes.
#    THIS IS DESTRUCTIVE. Verify the backup first.
docker volume rm \
    georag_postgres_data \
    georag_redis_data \
    georag_neo4j_data \
    georag_qdrant_data \
    georag_seaweedfs_data \
    georag_seaweedfs_filer_data \
    georag_dagster_home \
    georag_ragflow_data \
    georag_fastapi_hf_cache \
    georag_prometheus_data \
    georag_grafana_data \
    georag_alertmanager_data \
    georag_loki_data \
    georag_promtail_positions

# 4. Bring up only Postgres + Redis to start the restore.
docker compose --profile dev-data up -d postgresql redis pgbouncer

# 5. Apply schema.
docker compose exec laravel-octane php artisan migrate --force

# 6. Restore data per backup-restore.md.
bash ops/runbooks/backup-restore.md   # follow that runbook

# 7. Bring up the rest of the stack.
docker compose --profile dev-light up -d

# 8. Verify everything's healthy:
docker compose ps
curl -fsS http://localhost:8888/up
curl -fsS http://localhost:8000/health
bash database/tests/pgtap/run.sh
```

**Expected restore time on the dev workstation**: ~15 minutes for a
moderate dataset (10k drill holes, 100 documents). Production DR with
TB-scale data is hours; plan accordingly.

## Volume resize

Loki / Prometheus retention bumps are the common reason.

```bash
# 1. Stop the service.
docker compose stop loki

# 2. Inspect current size.
docker system df -v | grep loki_data

# 3. There's no in-place resize for a Docker named volume. Two options:
#    - Switch the volume's storage driver (e.g. local-persist).
#    - Move data out, recreate volume larger via host-bind, copy back.

# Most operators just bind-mount a host directory under their preferred
# filesystem and skip Docker volume management. Edit docker-compose.yml:
volumes:
  loki_data:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /mnt/loki-storage    # operator-chosen path

# 4. docker compose down && docker volume rm georag_loki_data
# 5. docker compose up -d loki
```

Coordinate retention shrink with the LogQL query window users expect.

## Cross-host migration

Out of V1 scope — single-node by design. V1.5+ when this matters.

## Disaster recovery

If a volume goes bad (disk failure, corruption):

```bash
# 1. Stop the service writing to it.
docker compose stop <service>

# 2. Confirm the corruption is in the volume, not elsewhere.
docker run --rm -v georag_<volume>:/data alpine ls -la /data
# If the files are unreadable / empty / partial, the volume is bad.

# 3. Wipe + recreate.
docker volume rm georag_<volume>
# (compose recreates on next up)
docker compose up -d <service>

# 4. Restore from backup per the service-specific runbook.
```

## Audit trail

Volume operations are operator-only and don't go through the audit
channel automatically. Record manually if a volume migration is
incident-related:

```bash
docker compose exec laravel-octane php artisan tinker
>>> Log::channel('authz_audit')->info('volume_migration', [
...     'volume' => 'fastapi_hf_cache',
...     'action' => 'recreate_for_uid_change',
...     'actor' => 'kyle@example.com',
...     'incident' => 'module-9-9.7-rollout',
... ]);
```

## Cross-references

- `ops/runbooks/container-hardening.md` — UID conventions per service.
- `ops/runbooks/backup-restore.md` — restore from backup procedures.
- `ops/runbooks/cold-start.md` — full-stack startup checklist.
- `ops/runbooks/log-retention.md` — Loki + authz_audit retention math.
- Memory `project_module_1_status.md` — C1 (volume-wipe cold-start) status.
- Memory `project_module_9_status.md` — 9.7 carry-forward this runbook closes.
