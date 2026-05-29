# Secret Management Runbook

**Tool chosen: SOPS + age**

**Rationale:**

| Criterion | SOPS + age | Doppler (SaaS) | HashiCorp Vault |
|---|---|---|---|
| License | Mozilla Public License 2.0 (SOPS), Apache 2.0 (age) — both permissive for on-prem use | Proprietary SaaS | BSL 1.1 (self-hosted Community Edition) |
| On-prem fit | Excellent — encrypts files in git, no network dependency | Requires Doppler SaaS reachability | Requires running a Vault cluster |
| Mining-client trust | High — encrypted `.env.production` lives in the same repo; auditors can verify | Low — data held externally | Medium — self-hosted but operationally complex |
| Operator skill | Low — `sops --encrypt` / `sops --decrypt` once, then normal `.env` editing | Very low | High — Vault policies, leases, seal/unseal |
| CI/CD integration | One `sops -d .env.production.enc > .env.production` step | Doppler CLI inject | Vault Agent sidecar or `vault kv get` |

SOPS with age is the correct choice for GeoRAG: free, MIT/MPL-2 licensed, self-hosted, no SaaS dependency, and the encrypted file can be committed to the git repository so the complete deployment state is version-controlled.

MPL-2.0 note: MPL-2 is a "weak copyleft" license that applies file-by-file. GeoRAG does not distribute SOPS itself (it is a toolchain binary, not a linked library), so there is no copyleft obligation. The architecture doc's "free-licensing only" stance is satisfied — SOPS is free software with no commercial restriction on use.

---

## Prerequisites

Install on the operator workstation (WSL Ubuntu):

```bash
# age key generator
sudo apt-get install -y age

# SOPS
SOPS_VERSION=3.9.4
curl -fsSL "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops_${SOPS_VERSION}_linux_amd64.deb" \
  -o /tmp/sops.deb
sudo dpkg -i /tmp/sops.deb
sops --version
```

CI runners install via the same commands or use the `mozilla/sops` GitHub Action.

---

## Bootstrap: provision a new environment

### 1. Generate an age key pair

```bash
# Generate the operator key
age-keygen -o ~/.config/georag/age-key.txt

# The file contains:
#   # created: 2026-04-26T...
#   # public key: age1...
#   AGE-SECRET-KEY-1...

# Extract the public key (safe to share / commit)
age-keygen -y ~/.config/georag/age-key.txt
# Output: age1xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Store the **private key** (`AGE-SECRET-KEY-1...`) in a password manager (BitWarden, 1Password) or hardware security module. Never commit the private key.

For team deployments, generate one key per operator and one key for the CI runner. All public keys are listed in `.sops.yaml`.

### 2. Create `.sops.yaml` at the repo root

```yaml
# .sops.yaml — public keys that can decrypt production secrets.
# Add a new entry per operator or CI runner key.
creation_rules:
  - path_regex: \.env\.production\.enc$
    age: >-
      age1<operator-1-public-key>,
      age1<operator-2-public-key>,
      age1<ci-runner-public-key>
```

Commit `.sops.yaml` to the repository. It contains only public keys — safe to share.

### 3. Prepare and encrypt the production env file

```bash
# Start from the template
cp .env.production.example .env.production

# Fill in all CHANGE_ME_VAULT_REFERENCE placeholders
# Use a text editor — do this on a machine with no network logging
nano .env.production

# Encrypt with SOPS (reads public keys from .sops.yaml)
sops --encrypt .env.production > .env.production.enc

# Verify the encrypted file is safe to commit
head -5 .env.production.enc
# Should show SOPS metadata + encrypted value blocks — no plaintext secrets

# Delete the plaintext version
rm .env.production

# Commit the encrypted file
git add .env.production.enc .sops.yaml
git commit -m "chore: add encrypted production env"
```

The plaintext `.env.production` MUST be in `.gitignore`. Verify:

```bash
grep "\.env\.production$" .gitignore
# Must match. If absent, add it:
echo ".env.production" >> .gitignore
```

---

## Decrypting for deployment

### Manual operator deployment

```bash
# Set SOPS_AGE_KEY_FILE to your private key
export SOPS_AGE_KEY_FILE=~/.config/georag/age-key.txt

# Decrypt to a temp file
sops --decrypt .env.production.enc > .env.production

# Deploy
docker compose --env-file .env.production --profile dev-light up -d --pull always

# Delete the plaintext immediately after deploy
rm .env.production
```

### CI/CD: GitHub Actions integration

The CI runner needs its own age private key stored as a GitHub Actions secret.

**Setup (one-time, performed by Kyle):**

1. Generate a CI-specific age key pair:
   ```bash
   age-keygen -o /tmp/ci-age-key.txt
   # Note the public key printed to stdout
   ```
2. Add the public key to `.sops.yaml` under `age:` (commit this change).
3. Re-encrypt `.env.production` so the CI key can decrypt:
   ```bash
   export SOPS_AGE_KEY_FILE=~/.config/georag/age-key.txt
   sops --rotate --in-place --add-age age1<ci-public-key> .env.production.enc
   git add .env.production.enc && git commit -m "chore: add CI age key to sops recipients"
   ```
4. In GitHub UI: Settings → Secrets → Actions → New repository secret:
   - Name: `SOPS_AGE_PRIVATE_KEY`
   - Value: contents of `/tmp/ci-age-key.txt` (the full `AGE-SECRET-KEY-1...` line)
5. Delete `/tmp/ci-age-key.txt`.

**In `cd.yml` deploy job:**

```yaml
- name: Decrypt production secrets
  env:
    SOPS_AGE_KEY: ${{ secrets.SOPS_AGE_PRIVATE_KEY }}
  run: |
    # SOPS reads key from SOPS_AGE_KEY env var (no file needed in CI)
    sops --decrypt .env.production.enc > .env.production
    # .env.production is in .gitignore and lives only in runner temp dir

- name: Deploy stack
  run: |
    docker compose --env-file .env.production --profile dev-light up -d --pull always
    rm .env.production  # destroy plaintext immediately
```

The `SOPS_AGE_KEY` environment variable is consumed by SOPS directly. GitHub Actions masks secrets in logs. The plaintext `.env.production` never touches the git working tree on the runner.

---

## Rotation runbook: rotating one secret

Example: rotate `POSTGRES_PASSWORD`.

### Step 1 — Decrypt locally

```bash
export SOPS_AGE_KEY_FILE=~/.config/georag/age-key.txt
sops --decrypt .env.production.enc > .env.production
```

### Step 2 — Generate a new secret

```bash
NEW_PG_PASS=$(openssl rand -base64 32)
echo "New POSTGRES_PASSWORD: $NEW_PG_PASS"
```

### Step 3 — Update the plaintext file

```bash
# Replace the old value in .env.production
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${NEW_PG_PASS}|" .env.production
# Also update DB_PASSWORD to match
sed -i "s|^DB_PASSWORD=.*|DB_PASSWORD=${NEW_PG_PASS}|" .env.production
```

### Step 4 — Apply the change to PostgreSQL

```bash
# Connect to PostgreSQL directly (bypass PgBouncer for DDL)
docker compose exec postgresql psql -U georag -d georag \
  -c "ALTER USER georag WITH PASSWORD '${NEW_PG_PASS}';"
```

### Step 5 — Re-encrypt and commit

```bash
sops --encrypt .env.production > .env.production.enc
rm .env.production
git add .env.production.enc
git commit -m "chore(secrets): rotate POSTGRES_PASSWORD [skip ci]"
```

### Step 6 — Redeploy affected services

```bash
# Services that hold a PgBouncer connection pool need a restart
docker compose restart pgbouncer laravel-octane laravel-horizon fastapi dagster-daemon dagster-webserver
```

### Step 7 — Verify

```bash
# Check Laravel can reach the DB
docker compose exec laravel-octane php artisan db:show

# Check FastAPI health
curl http://localhost:8000/health
```

---

## Rotation cadence

| Secret | Rotation cadence | Notes |
|---|---|---|
| `POSTGRES_PASSWORD` | 90 days or on personnel change | Follow Step 4 ALTER USER |
| `REDIS_PASSWORD` | 90 days | requirepass + CONFIG SET |
| `APP_KEY` | On personnel change only | Laravel re-encrypts sessions; all users re-authenticate |
| `FASTAPI_SERVICE_KEY` | 90 days | Requires simultaneous restart of Laravel + FastAPI |
| `REVERB_APP_KEY` / `REVERB_APP_SECRET` | 90 days | Frontend must rebuild (VITE_REVERB_APP_KEY is baked into JS bundle) |
| `NEO4J_PASSWORD` | 90 days | ALTER CURRENT USER SET PASSWORD |
| `QDRANT_API_KEY` | 90 days | Update qdrant service + FastAPI client |
| `MINIO_ROOT_PASSWORD` | 90 days | SeaweedFS admin credential |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | 90 days | Backup upload credentials |
| `GRAFANA_ADMIN_PASSWORD` | 90 days | Grafana UI password reset |
| `ANTHROPIC_API_KEY` | Per Anthropic policy | Rotate in Anthropic console, update here |
| `HF_TOKEN` | On personnel change | HuggingFace token revoke + reissue |

---

## Checking for plaintext secret leakage

Before every commit:

```bash
# Ensure .env.production is gitignored
git status | grep "\.env\.production$"
# Must show as untracked (ignored), never as "modified" or "new file"

# Scan staged files for common secret patterns
git diff --cached | grep -iE "(password|secret|api_key|token)\s*=\s*[A-Za-z0-9+/=]{16,}"
# Empty output = clean
```

Consider adding `gitleaks` as a pre-commit hook for automated scanning:

```bash
# Install gitleaks (MIT license)
curl -fsSL https://github.com/gitleaks/gitleaks/releases/download/v8.24.0/gitleaks_8.24.0_linux_x64.tar.gz \
  | tar xz -C /usr/local/bin gitleaks

# Add pre-commit hook
cat > .git/hooks/pre-commit << 'EOF'
#!/bin/sh
gitleaks protect --staged --redact -q
EOF
chmod +x .git/hooks/pre-commit
```

---

## Items not yet closed (TODO for Kyle)

1. **GitHub Actions secrets provisioned**: `SOPS_AGE_PRIVATE_KEY` must be added to the repo via GitHub Settings → Secrets before `cd.yml` deploy jobs can decrypt secrets. Kyle performs this once after generating the CI age key pair.

2. **`STAGING_SSH_HOST`, `PRODUCTION_SSH_HOST` secrets**: The `cd.yml` workflow uses SSH to deploy to target hosts. Kyle must add these secrets to the GitHub repo (per-environment) once staging/production hosts are provisioned. See `cd.yml` TODO comments.

3. **Self-hosted runner for GPU tests**: `release-rehearsal.yml` golden + hallucination tests require a runner with an NVIDIA GPU and the Ollama stack. Once a self-hosted runner is registered, flip `continue-on-error: false` on those jobs and remove the TODO comment.
