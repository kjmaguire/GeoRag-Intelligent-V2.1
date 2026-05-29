# Phase H4 — Operator Deploy Checklist

Step-by-step procedure for promoting the Phase H4 admin surfaces to a
production environment. Every Phase H4 commit prior to this file has
been verified against `scripts/phase_h4_acceptance.sh` returning 13/13
PASS on the dev stack.

This is a **forward-only** rollout — there is no Phase H4 disable flag.
Surfaces become reachable as soon as Laravel + FastAPI containers are
re-deployed.

## Pre-flight (do BEFORE touching production)

- [ ] Pull the latest `main` and verify HEAD matches the merge commit you
  intend to deploy:
  ```bash
  git fetch origin && git log --oneline -1 origin/main
  ```
- [ ] Confirm the dev-stack acceptance harness is green:
  ```bash
  export FASTAPI_SERVICE_KEY=$(docker exec georag-fastapi bash -c 'echo $FASTAPI_SERVICE_KEY')
  ./scripts/phase_h4_acceptance.sh
  ```
  Expected: `Phase H4 acceptance: 13 / 13 checks passed`.
- [ ] `FASTAPI_SERVICE_KEY` is set + identical in:
  - Laravel `.env`
  - FastAPI container env (docker-compose or k8s secret)
  - Hatchet worker container envs (they share the FastAPI image)
- [ ] You know the value of the secret well enough to compare with
  `hash_equals`-style logic in your head. If you don't, rotate it —
  the rotation procedure is in `docs/RUNBOOK.md`.

## 1 — Database migrations

Two new SQL files. Both are idempotent and additive; rolling back means
dropping the tables/indexes manually (Phase H4 does not generate any
data the older code reads).

```bash
# Migration 101: three new tables for cross-workspace admin registries.
psql "$DATABASE_URL" < database/raw/phase0/101-phase-h4-ui-tables.sql

# Migration 102: two partial indexes on audit_ledger. Production
# deploys with an existing populated audit_ledger MUST use
# CONCURRENTLY (see file's comment block for the exact statements).
psql "$DATABASE_URL" < database/raw/phase0/102-phase-h4-alerts-index.sql
```

Verify:

```bash
psql "$DATABASE_URL" -c "\dt silver.qp_credentials"
psql "$DATABASE_URL" -c "\dt silver.workspace_settings"
psql "$DATABASE_URL" -c "\dt workflow.activepieces_channels"
psql "$DATABASE_URL" -c "SELECT indexname FROM pg_indexes WHERE indexname LIKE 'audit_ledger_%_idx' ORDER BY indexname"
```

## 2 — FastAPI container rebuild + redeploy

```bash
docker compose build fastapi
docker compose up -d fastapi
docker compose restart hatchet-worker-ai hatchet-worker-ingestion
```

Wait ~30 s for the health check to pass, then smoke the new endpoints:

```bash
docker compose exec fastapi curl -s -H "X-Service-Key: $FASTAPI_SERVICE_KEY" \
  http://localhost:8000/api/v1/admin/alerts-inbox?limit=1
```

Expected: `{"items":[],"total":0}` (or your existing audit-anchored alerts).

## 3 — Laravel image rebuild + redeploy

```bash
docker compose build laravel-octane
docker compose up -d laravel-octane laravel-horizon laravel-reverb

# Clear Octane's cached routes + config. NEVER skip this — Octane boots
# once and caches the route table.
docker compose exec laravel-octane php artisan optimize:clear
docker compose exec laravel-octane php artisan route:cache
docker compose exec laravel-octane php artisan config:cache
```

Verify the new routes register:

```bash
docker compose exec laravel-octane php artisan route:list --path=alerts-inbox
docker compose exec laravel-octane php artisan route:list --path=reports/builds
docker compose exec laravel-octane php artisan route:list --path=target-recommendation/runs
docker compose exec laravel-octane php artisan route:list --path=internal
```

You should see the 4 new routes:
- `admin.alerts-inbox`
- `admin.alerts-inbox.ack`
- `admin.reports.save-section`
- `admin.target-recommendation.geojson`
- `internal.reports.progress`

## 4 — Frontend bundle

The new Inertia pages (`AlertsInbox.tsx`, updates to `ReportBuild.tsx`,
`SavedMaps.tsx`, `TargetRecommendationCockpit.tsx`) require a fresh
build. Components import `maplibre-gl@^5.23.0` which is already pinned.

```bash
npm ci
npm run build
```

Verify the new bundle hash in `public/build/manifest.json` — Inertia
falls over silently if the manifest doesn't include the new pages.

## 5 — Reverb / broadcast bridge

The Phase H4 cockpit subscribes to `private-admin.reports.{build_id}`.
Verify Reverb is reachable from the Laravel-Octane container:

```bash
docker compose exec laravel-octane php artisan tinker --execute \
  'event(new \App\Events\Admin\ReportBuildProgress("smoke", "test"));'
```

No exception = the event class loads + the broadcast queue is configured.

## 6 — Post-deploy smoke

Run the same acceptance harness against the production endpoint (with
`FASTAPI_URL=https://...` set):

```bash
FASTAPI_URL="https://georag.example.com" \
FASTAPI_SERVICE_KEY="<prod key>" \
PG_CONTAINER=<your prod pg container or psql-installed bastion> \
./scripts/phase_h4_acceptance.sh
```

Expected: 15/15 PASS. If any check fails:

| Failure                                          | Likely cause                                    | Fix                                                    |
|--------------------------------------------------|-------------------------------------------------|--------------------------------------------------------|
| `GET …/qp-credentials` returns 500               | Migration 101 didn't run                        | Re-run step 1                                          |
| `GET …/workspace-members` returns empty (warn)   | workspace schema not seeded                     | Expected on fresh installs — not an error              |
| Service-key gate returns 200                     | FASTAPI_SERVICE_KEY mismatch or middleware mis-aliased | Verify `bootstrap/app.php` registers `'service.key'`   |
| §7 round-trip 500 on PUT                         | No workspace in `silver.workspaces`             | Seed at least one workspace (operator action)          |
| Alerts inbox 422 on POST ack                     | UUID validation on `audit_id`                   | Operator sent a non-UUID — expected behaviour          |
| `phase-h4-health` reports any check `ok=false`   | varies per check.detail                         | follow the inline hint each check carries              |

### Single-call composite health

After the acceptance harness, hit `GET /admin/phase-h4-health` (or
open the Inertia page at the same URL). It checks 10 dependencies in
one call:

- `pg_pool initialised`
- Each Phase H4 table present (qp_credentials, workspace_settings,
  activepieces_channels, audit_ledger, target_candidate_zones)
- `silver.workspace_settings` RLS enabled + forced
- Partial indexes `audit_ledger_alerts_idx` + `audit_ledger_acks_idx`
- `FASTAPI_SERVICE_KEY` env var present

Each check carries an inline `detail` pointing at the migration or
config knob to inspect if it fails.

### Audit-chain integrity smoke

Before declaring the deploy clean, run the chain verifier against
the last hour of audit rows (the harness does the live window for
synthetic data; this is the prod-history check):

```bash
curl -H "X-Service-Key: $FASTAPI_SERVICE_KEY" \
    "$FASTAPI_URL/api/v1/admin/audit-explorer/verify-chain?limit=10000"
```

Expected: `{"continuous": true, "rows_verified": N, ...}`. A
`continuous: false` response means the audit chain broke during
deploy — surface to security review immediately.

## 7 — User-visible changes

After successful deploy operators will see:

- `/admin/phase-h4-health` — new operator page; one-shot dependency
  check with refresh button. Open first when something looks off.
- `/admin/alerts-inbox` — new page, severity-filtered audit alerts
  inbox; "Verify audit chain (24h)" button at the top runs the
  hash-chain integrity verifier on demand.
- `/admin/reports/{build_id}` — sections rendered as editable textareas
  with last-saved timestamp + Save button per section.
- `/admin/target-recommendation/runs/{run_id}` — MapLibre panel above
  the ranked-targets list; clicking a zone selects it in the list.
- `/admin/saved-maps` — Restore button on each row navigates to
  `/explorer` with the saved center/zoom/layer state applied.
- Live build progress strip on `/admin/reports/{build_id}` when a
  generate_report workflow is running.

## 8 — Rollback (if required)

```bash
# Revert Laravel and FastAPI images (specify pre-Phase-H4 SHAs):
git checkout <pre-h4-sha>
docker compose build fastapi laravel-octane
docker compose up -d

# Drop the migration artifacts (only if you want to fully purge):
psql "$DATABASE_URL" -c "
  DROP INDEX IF EXISTS audit.audit_ledger_alerts_idx;
  DROP INDEX IF EXISTS audit.audit_ledger_acks_idx;
  DROP TABLE IF EXISTS silver.qp_credentials;
  DROP TABLE IF EXISTS silver.workspace_settings;
  DROP TABLE IF EXISTS workflow.activepieces_channels;
"
```

Rolling forward again is harmless — the migrations are `CREATE … IF NOT EXISTS`.
