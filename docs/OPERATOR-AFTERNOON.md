# Operator Afternoon — First Prod Deploy Setup

Run-once checklist that takes V1 from "engineering done" to "deployable".
Everything in this doc is operator-side. Engineering scope is closed.

Allow ~3 hours. All steps are reversible until step 8 (the actual deploy).

**Prerequisites on operator workstation:**
- WSL Ubuntu (or any Linux) with `bash`, `curl`, `git`
- `gh` CLI authenticated (`gh auth status`)
- Repo cloned, on `main`, clean working tree
- Password manager open (BitWarden / 1Password) for storing the age private key

---

## Step 0 — Sanity check

```bash
cd "/home/Development/GeoRAG Intelligence V.1.0"
bash scripts/operator/preflight.sh
```

Expect: red Xs on items O-01..O-07. That's fine — the script tells you what's
missing. We'll close them in order.

---

## Step 1 — O-01 + O-04: Bootstrap SOPS + age, encrypt prod env

```bash
bash scripts/operator/bootstrap-secrets.sh
```

The script will:
1. Install `age` and `sops` if missing (apt + GitHub release).
2. Generate an operator age key at `~/.config/georag/age-key.txt` (skipped if it exists).
3. Generate a CI age key at `~/.config/georag/age-key-ci.txt`.
4. Write `.sops.yaml` with both public keys.
5. Pause and let you fill `.env.production` from `.env.production.example`.
6. Encrypt `.env.production` → `.env.production.enc` and delete the plaintext.
7. Print the CI private key (one time only) for you to paste into the GitHub Secret.

**Action between steps 5 and 6:** open `.env.production` in a text editor and
replace every `CHANGE_ME_VAULT_REFERENCE`. Generation hints:

```bash
# APP_KEY (Laravel)
docker compose run --rm laravel-octane php artisan key:generate --show

# 32-byte random secrets (most _PASSWORD / _SECRET / _TOKEN keys)
openssl rand -base64 32
```

Confirm `APP_DEBUG=false`, `APP_ENV=production`, `MULTI_TENANT_ENFORCEMENT_ENABLED=true`,
`SINGLE_TENANT_MODE=false`. (`bootstrap-secrets.sh` greps for these and warns
if they're wrong.)

Then commit:

```bash
git add .sops.yaml .env.production.enc
git commit -m "chore(secrets): bootstrap SOPS recipients + encrypted prod env"
```

**Store both `~/.config/georag/age-key.txt` and `~/.config/georag/age-key-ci.txt`
in your password manager NOW.** They are the only key to the encrypted env file.

---

## Step 2 — O-01 + O-02 + O-03: Push GitHub Secrets

```bash
bash scripts/operator/set-github-secrets.sh
```

The script prompts for each secret in order and pushes via `gh secret set`.
You'll need values for:

| GitHub Secret | Source | Notes |
|---|---|---|
| `SOPS_AGE_PRIVATE_KEY` | contents of `~/.config/georag/age-key-ci.txt` | repo-level secret |
| `DEV_SSH_HOST` / `DEV_SSH_USER` / `DEV_SSH_KEY` | dev host hostname, ssh user, ED25519 private key | env: `dev` |
| `STAGING_SSH_HOST` / `STAGING_SSH_USER` / `STAGING_SSH_KEY` | staging host trio | env: `staging` |
| `PRODUCTION_SSH_HOST` / `PRODUCTION_SSH_USER` / `PRODUCTION_SSH_KEY` | prod host trio | env: `production` |
| `STAGING_URL` | https://staging.<your-domain> | repo-level |
| `STAGING_BASE_URL` / `PRODUCTION_BASE_URL` | same as above with no path | optional, used by health checks |

If a host isn't provisioned yet, leave blank — the script will skip it and
the cd.yml job stays a graceful no-op for that environment.

**SSH key generation (per host, run once):**

```bash
ssh-keygen -t ed25519 -C "georag-deploy@$(hostname)" -f ~/.ssh/georag_deploy_<env>
# Copy ~/.ssh/georag_deploy_<env>.pub into the host's /opt/georag/.ssh/authorized_keys
# Paste the contents of ~/.ssh/georag_deploy_<env> (private) when set-github-secrets.sh asks
```

---

## Step 3 — Configure GitHub Environments + reviewers

In the GitHub UI: **Settings → Environments**

- Create `dev` — no approval required.
- Create `staging` — add yourself (Kyle) as required reviewer.
- Create `production` — add required reviewer + enable "Require branches: main".

The cd.yml workflow already references these environments by name; this step
activates the manual approval gates.

---

## Step 4 — Flip cd.yml from no-op to enforcing

Once GitHub Secrets are populated, remove the `continue-on-error: true`
guards from the deploy/health-check steps so a real failure becomes a real
red build.

```bash
bash scripts/operator/preflight.sh --emit-cd-patch | tee /tmp/cd-patch.diff
# Review the diff
git apply /tmp/cd-patch.diff
git add .github/workflows/cd.yml
git commit -m "ci(cd): enforce deploy gates after operator provisioning"
```

The patch removes 9 `continue-on-error: true` lines plus their TODO comments.

---

## Step 5 — Wire Alertmanager (O-07)

On the production host:

```bash
cd /opt/georag
cp docker/alertmanager/alertmanager.production.yml.example \
   docker/alertmanager/alertmanager.production.yml

# Edit and substitute:
#   ${SLACK_WEBHOOK_URL}     → real webhook from #georag-alerts channel
#   ${PAGERDUTY_ROUTING_KEY} → routing key from PagerDuty service
#   channel placeholders     → real channel names
nano docker/alertmanager/alertmanager.production.yml

# The prod compose override mounts this file over the dev alertmanager.yml.
# No service restart needed if alertmanager is not yet running.
```

This file is **not** committed (it has a webhook URL). Add to operator
runbook handoff if multiple operators.

---

## Step 6 — Cold-start the prod stack (O-05)

```bash
# On the production host, follow ops/runbooks/cold-start.md.
# Summary: 10 steps from empty volumes → migrated DB → seed → smoke.
```

After cold-start, the stack should be healthy on `https://<prod-host>/up`
and `https://<prod-host>:8000/health`.

---

## Step 7 — Trigger the first deploy

From your workstation:

```bash
gh workflow run cd.yml -f target=dev
gh run watch
```

Then promote dev → staging → production via the manual approval gates in the
GitHub UI.

---

## Step 8 — O-06: Capture first perf-baseline

The nightly `perf-baseline.yml` workflow fires at 02:00 UTC against
`STAGING_URL`. After the first run:

```bash
gh run download $(gh run list --workflow=perf-baseline.yml --limit 1 --json databaseId -q '.[0].databaseId')
# Inspect the YAML diff against ops/baselines/2026-04-22-api-latency.md
git add ops/baselines/2026-04-22-api-latency.md
git commit -m "ops(baselines): capture first nightly perf-baseline"
```

Future nightly runs auto-fail on >20% regression.

---

## Step 9 — Re-run preflight

```bash
bash scripts/operator/preflight.sh
```

Expect green checks on O-01..O-07. The acceptance-criteria.md ⏳ rows
auto-flip to ✅ on the next CI run that includes this commit.

---

## What this does NOT cover

- **D2 Drillhole rename (V-05):** schedule maintenance window, run
  `ops/migrations/neo4j/2026-04-27-drillhole-rename.cypher` per
  `ops/runbooks/drillhole-label-rename.md`.
- **Self-hosted runner for e2e (V-03):** separate provisioning task; not
  required for first deploy.
- **arm64 build merge (V-01):** fires automatically on next merge to main.
- **Helm cluster install (V-04):** only if a client requests k8s deploy.

These are sequencing gates that fire after the corresponding infra lands —
not blockers for the first prod deploy.

---

## Rollback for any step

Every step in this checklist is reversible until Step 8.

- **Bootstrap script:** delete `~/.config/georag/age-key*.txt` and
  `.env.production.enc`; re-run from clean.
- **GitHub Secrets:** `gh secret delete <NAME>` (or set to a new value).
- **cd.yml flip:** `git revert` the commit.
- **Alertmanager wiring:** rm the production yaml on the host.
- **Cold-start:** `ops/runbooks/volume-migration.md` covers the wipe-and-redo.
- **First deploy:** `ops/runbooks/deploy-rollback.md`.
