# Deploy Rollback

**Module 10 Chunk 10.8** — revert a bad production deploy in under 5 minutes.
Closes the TODO referenced from `cd.yml` Module 10 Chunk 10.2.

## TL;DR

```bash
# Grab the previous good SHA from CD history.
PREV_SHA=$(gh run list -w cd.yml --limit 5 --json conclusion,headSha,name \
    | jq -r '.[] | select(.conclusion=="success") | .headSha' \
    | head -2 | tail -1 | cut -c1-7)

# Trigger CD with explicit SHA.
gh workflow run cd.yml \
    --field target=production \
    --field sha=$PREV_SHA
```

The `cd.yml` workflow accepts a `sha` input (Module 10 Chunk 10.2 added
this) — pass the previous-good SHA and the deploy job pulls those images
from GHCR + restarts.

## When to roll back

Rolling back a deploy reverts:
- App images (Laravel, FastAPI, Dagster) to the previous SHA tag.
- Encrypted env file via SOPS — same rollback if the env was edited in the
  failed deploy.

It does NOT revert:
- Database migrations applied by the deploy. See `migration-rollback.md`
  for that — typically you revert app first, then schema, in that order.
- Object storage writes (SeaweedFS) made by the new code.
- Redis cache state.

## Triage decision tree

```
Production deploy fails health check OR users complain?
│
├─ App boots but feature broken
│  └─ FIX FORWARD if patchable in <30 min, else ROLL BACK
│
├─ App refuses to boot (boot guard fires, container restart loop)
│  └─ ROLL BACK immediately
│
├─ Subset of users affected (cross-tenant or A/B-flag scoped)
│  └─ FLIP FLAG OFF (faster than rollback) — see flag-management.md (TODO)
│
└─ Performance regression > 50%
   └─ ROLL BACK; investigate post-mortem
```

## Rollback procedure

### Step 1: identify previous good SHA

```bash
# List recent CD runs.
gh run list -w cd.yml --limit 10

# Or via the GitHub UI:
#   Actions → CD — Deploy → success runs in the last 7 days.

# Take the last green SHA. If you're unsure, check the docker image:
docker compose --profile prod images
```

### Step 2: trigger rollback deploy

```bash
gh workflow run cd.yml \
    --field target=production \
    --field sha=$PREV_SHA
# Watch:
gh run watch
```

`cd.yml`'s `deploy_production` job:
1. SSHes to the production host.
2. Pulls SHA-tagged images from GHCR.
3. `docker compose --profile dev-light up -d --pull always`.
4. Health-poll loop (`/health` + `/up` for 5 min).
5. Smoke-test integration suite.

If health-poll fails the rollback, the workflow exits non-zero and you
need to escalate — see "Stuck rollback" below.

### Step 3: verify

```bash
# Health endpoints from outside the production network.
curl -fsS https://prod.example.com/up
curl -fsS https://prod.example.com/health    # FastAPI behind a proxy

# Authz audit shouldn't show 5xx spikes.
# Open Grafana → "GeoRAG — Authorization (authz.deny)" → last 1h
```

### Step 4: post-rollback

Open a GitHub issue tagged `deploy-regression` with:
- Failed SHA + the error symptom.
- Rollback SHA.
- Rollback timestamp.
- Triage findings (if any) — what made the deploy bad?

The issue title format the on-call rotation expects:
`deploy-regression: <one-line symptom> @ <date>`

## Stuck rollback

If `cd.yml` rollback fails:

### Manually pull the previous image

```bash
# SSH to production host as the deploy user.
ssh deploy@prod.example.com

cd /opt/georag
git fetch
git checkout $PREV_SHA

# Hand-pull images.
docker pull ghcr.io/$OWNER/georag-fastapi:$PREV_SHA
docker pull ghcr.io/$OWNER/georag-laravel:$PREV_SHA
docker pull ghcr.io/$OWNER/georag-dagster:$PREV_SHA

# Restart with the new tag.
GEORAG_IMAGE_TAG=$PREV_SHA docker compose up -d
```

### If the encrypted env was changed too

```bash
git checkout $PREV_SHA -- .env.production.enc
sops --decrypt .env.production.enc > .env.production
docker compose down && docker compose --env-file .env.production up -d
```

## Database considerations

If the failed deploy applied migrations and the rollback reverts the app
image, the schema and the app are now out-of-sync. Two paths:

### Path A: app+schema rollback together (preferred)

```bash
# 1. Roll back the app first.
gh workflow run cd.yml --field sha=$PREV_SHA --field target=production

# 2. Roll back migrations on the production DB.
ssh deploy@prod.example.com 'docker compose exec -T laravel-octane php artisan migrate:rollback --step=N'

# 3. Verify pgTAP green.
ssh deploy@prod.example.com 'cd /opt/georag && bash database/tests/pgtap/run.sh'
```

See `migration-rollback.md` for the migration-side detail.

### Path B: forward-fix the schema, no migration rollback

If the new migration is sound but the app code that consumes it is broken,
revert just the app image. The DB stays at the new schema; the older app
ignores the new column. Works only for additive migrations (the common case).

If the migration is destructive (drops or NOT-NULLs an existing column),
you can't ship the older app — Path A is forced.

## Cross-references

- `.github/workflows/cd.yml` — the deploy pipeline being triggered.
- `ops/runbooks/migration-rollback.md` — schema rollback if needed.
- `ops/runbooks/secret-management.md` — env file rollback via SOPS.
- `ops/runbooks/on-call.md` — escalation path if rollback fails.
- `ops/runbooks/backup-restore.md` — last resort when rollback can't recover.

## Audit trail

Every rollback should log via:

```bash
docker compose exec laravel-octane php artisan tinker
>>> Log::channel('authz_audit')->warning('deploy_rollback', [
...     'from_sha' => 'abc1234',
...     'to_sha' => 'def5678',
...     'actor' => 'kyle@example.com',
...     'reason' => 'p95 regression > 50%'
... ]);
```

Query Loki:
```
{channel="authz_audit"} |= "deploy_rollback"
```
