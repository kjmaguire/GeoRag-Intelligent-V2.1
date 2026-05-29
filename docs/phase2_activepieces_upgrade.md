# Activepieces — image upgrade runbook

**Status:** Active operator runbook (Phase 2 R-P2-9).
**Owner:** Platform on-call.
**Companions:** `docs/phase2_activepieces_flows.md` (flow definitions),
`docs/phase2_handoff.md` (Phase 2 closing state).

---

## 1. When to run this

- A new Activepieces release ships features or security fixes you want.
- Quarterly housekeeping (LTS-style — even if no urgent need, don't drift more than ~6 months).
- A piece you depend on (Slack, HTTP, AWS S3, Code) requires a newer Activepieces engine.

Do **not** upgrade casually during a Phase 1 cutover window or while a
flow is mid-migration. The upgrade involves a service restart and (often)
a Postgres schema migration that runs on first boot of the new image.

---

## 2. Pre-flight

1. **Pick the target tag.** Browse [Docker Hub](https://hub.docker.com/r/activepieces/activepieces/tags)
   and read the [release notes](https://github.com/activepieces/activepieces/releases)
   for every version between the current pin and the target. Look for:
   - Schema migrations (typically called out in release notes)
   - Breaking changes to piece APIs you use
   - Auth / connection model changes
2. **Snapshot the activepieces DB.** Even though `pg_basebackup` runs
   nightly (R-P2-8), trigger one ad-hoc:
   ```bash
   docker exec -e DRY_RUN=0 georag-backup-agent /bin/bash /backup-scripts/postgresql/backup.sh
   ```
   Wait for completion + confirm the SeaweedFS upload landed.
3. **Export the current flows.**
   - Open `http://localhost:8090`
   - For each flow under `/flows`: `…` menu → Export → save the JSON.
   - Commit the JSONs to a private branch as a recovery artefact.
   - (R-P2-2 — Phase 3 — will automate this. Until then, do it manually.)
4. **Note the current pin** (the value in `docker-compose.yml`'s
   `activepieces:` block under `image:`).

---

## 3. The upgrade

### 3.1 Bump the pin

Edit `docker-compose.yml`:

```yaml
activepieces:
  image: activepieces/activepieces:0.84.0   # ← new tag here
```

Commit the bump with a message that names the prior + new tag:

```
chore(activepieces): bump 0.83.0 → 0.84.0
```

### 3.2 Pull + recreate

```bash
cd /home/georag/projects/georag
docker compose --profile dev-data pull activepieces
docker compose --profile dev-data up -d --force-recreate --no-deps activepieces
```

`--no-deps` keeps Postgres + Redis untouched. Activepieces handles its
own schema migration on first boot of the new image; this can take
**up to 60 seconds** for the first request — the healthcheck's
`start_period: 60s` already accounts for this, but if you bump that
upper bound on a major release, document the new value in the compose
comment.

### 3.3 Watch the boot

```bash
docker logs -f georag-activepieces 2>&1 | grep -iE 'migration|error|listening'
```

Look for:
- `migrations are up to date` (or equivalent — varies per release)
- `listening on :80` / `Server is ready`

If you see migration errors, **do not let the service serve traffic**
— roll back per §5.

### 3.4 Verify

Run the Phase 2 Step 2 verifier:

```bash
bash scripts/phase2_step2_verify.sh
```

Expected: 5/5. The healthcheck check (#2) takes up to 90s — that's
normal on a fresh start.

Then run Step 3:

```bash
bash scripts/phase2_step3_verify.sh
```

Expected: 6/6. This proves the FastAPI dispatch path still works
(Activepieces is downstream of FastAPI in the trigger flow).

Finally, exercise both flows:

```bash
bash scripts/phase2_step4_smoke.sh
bash scripts/phase2_step5_smoke.sh
```

Both expected to pass. If Step 4 fails on the upstream HTTP fetch and
Step 5 passes, the issue is upstream-feed or network — not Activepieces.

### 3.5 Re-import flows (if needed)

Some major Activepieces upgrades migrate flow definitions in-place.
Some require re-import. Check the release notes; if re-import is
required:

1. Disable each flow in the Activepieces UI.
2. Delete it.
3. Import the JSON exported in §2.3.
4. Re-enable. The webhook URL **may change** on re-import — update
   any external sender's configured URL.

---

## 4. Verification gate before declaring "upgrade done"

| Check | Expected | If not |
|------|---------|-------|
| `phase2_step2_verify.sh` | 5/5 | Service unhealthy — see §5 |
| `phase2_step3_verify.sh` | 6/6 | Dispatch broken — usually unrelated to Activepieces |
| `phase2_step4_smoke.sh` | PASSED | Outbound flow regression — see release notes |
| `phase2_step5_smoke.sh` | PASSED | Inbound flow regression — see release notes |
| `/admin/integrations` loads in browser | yes | Inertia page break — likely `npm run build` needed if you also bumped frontend deps |
| Webhook URL on Step 5 still works | yes | Activepieces may have rotated the secret — update sender |

Only declare done when **all six** are green.

---

## 5. Rollback

If §3.3 boot logs show migration errors, OR if §4 verifications fail
in a way you can't diagnose in <30 minutes:

### 5.1 Bring service down (don't let it serve)

```bash
docker compose --profile dev-data stop activepieces
```

### 5.2 Restore the DB

The activepieces DB schema migration may have partially applied.
Restore from §2.2's snapshot:

```bash
# 1. Stop the new container so nothing's writing
docker compose --profile dev-data stop activepieces

# 2. Drop + recreate the activepieces DB from the snapshot.
#    (This is a CLUSTER-LEVEL restore — see docker/postgresql/restore.sh
#    for the canonical procedure.)
```

Per `docs/RUNBOOK.md`'s Postgres restore section: a cluster-level
restore from `pg_basebackup` rolls **every** logical DB back to the
snapshot timestamp. If you don't want to roll georag + hatchet too,
you need a logical (`pg_dump`-based) restore of just `activepieces`,
which Phase 2 does NOT pre-provision. Take the cluster-level rollback
or accept the loss of the activepieces DB state since the snapshot.

### 5.3 Pin back

Revert the `docker-compose.yml` pin to the prior value (§2.4) and:

```bash
docker compose --profile dev-data pull activepieces
docker compose --profile dev-data up -d --force-recreate --no-deps activepieces
```

### 5.4 Re-verify

Run §4's gate. If still failing, escalate.

---

## 6. Post-upgrade

- Update the `image:` line's nearby comment in `docker-compose.yml`
  if you changed any healthcheck timing.
- Note the upgrade in the on-call channel: prior tag, new tag, any
  observed schema migrations, total downtime.
- If you exported + re-imported flows, commit the new JSONs to the
  same recovery branch.

---

## 7. Known gotchas

- **The image is large** (~600 MB compressed). First pull on a fresh
  environment can take 5+ minutes on a slow link.
- **First-request boot** triggers piece downloads. Activepieces caches
  these in `/usr/src/app/cache` (the `activepieces_cache` volume).
  After a major upgrade the cache may be invalidated — first run of
  each flow takes longer.
- **`/api/v1/flags` 200s before everything is ready.** The Step 2
  verifier checks this endpoint, but a 200 here doesn't guarantee
  flows are dispatchable. The Step 4/5 smokes are the real gate.
- **Webhook URLs may rotate.** Activepieces sometimes rotates webhook
  secrets across major versions. External senders see 404s until you
  hand them the new URL.
