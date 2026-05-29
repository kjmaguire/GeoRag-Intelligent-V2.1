#!/usr/bin/env bash
# set-github-secrets.sh — operator-side GitHub Secrets provisioner.
#
# Pushes secrets via the gh CLI for cd.yml + perf-baseline.yml + release-rehearsal.yml.
# Skips any secret left blank at the prompt; safe to re-run.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

CI_KEY="${HOME}/.config/georag/age-key-ci.txt"

c_red() { printf '\033[31m%s\033[0m\n' "$*"; }
c_grn() { printf '\033[32m%s\033[0m\n' "$*"; }
c_yel() { printf '\033[33m%s\033[0m\n' "$*"; }
c_blu() { printf '\033[34m%s\033[0m\n' "$*"; }

require_gh() {
  if ! command -v gh >/dev/null 2>&1; then
    c_red "ERROR: gh CLI not installed. https://cli.github.com/"
    exit 1
  fi
  if ! gh auth status >/dev/null 2>&1; then
    c_red "ERROR: gh CLI not authenticated. Run: gh auth login"
    exit 1
  fi
  c_grn "✓ gh CLI authenticated as: $(gh api user -q .login)"
}

# set_secret <name> <env-or-empty> <prompt>
# If env is empty, sets repo-level. Otherwise sets per-environment.
set_secret() {
  local name="$1" env="$2" prompt="$3"
  local current scope_label

  if [ -z "$env" ]; then
    scope_label="repo"
    current=$(gh secret list --json name -q ".[] | select(.name==\"$name\") | .name" 2>/dev/null || true)
  else
    scope_label="env:$env"
    current=$(gh secret list --env "$env" --json name -q ".[] | select(.name==\"$name\") | .name" 2>/dev/null || true)
  fi

  local hint=""
  [ -n "$current" ] && hint=" (already set — leave blank to keep)"

  c_blu ""
  c_blu "→ ${name} [${scope_label}]"
  c_yel "  ${prompt}${hint}"
  read -rsp "  Value (input hidden): " value
  echo
  if [ -z "$value" ]; then
    if [ -n "$current" ]; then
      c_grn "  ✓ kept existing value"
    else
      c_yel "  ⊝ skipped (no value provided, no existing secret)"
    fi
    return
  fi

  if [ -z "$env" ]; then
    printf '%s' "$value" | gh secret set "$name" --body -
  else
    ensure_environment "$env"
    printf '%s' "$value" | gh secret set "$name" --env "$env" --body -
  fi
  c_grn "  ✓ ${name} set on ${scope_label}"
}

# set_secret_from_file <name> <env-or-empty> <file>
set_secret_from_file() {
  local name="$1" env="$2" file="$3"
  if [ ! -f "$file" ]; then
    c_yel "  ⊝ ${name}: source file ${file} missing; skipping"
    return
  fi
  if [ -z "$env" ]; then
    gh secret set "$name" < "$file"
  else
    ensure_environment "$env"
    gh secret set "$name" --env "$env" < "$file"
  fi
  c_grn "  ✓ ${name} set from ${file}"
}

ensure_environment() {
  local env="$1"
  # gh api creates the environment if it doesn't exist; idempotent
  local repo
  repo=$(gh repo view --json nameWithOwner -q .nameWithOwner)
  gh api -X PUT "repos/${repo}/environments/${env}" >/dev/null 2>&1 || true
}

main() {
  c_blu "GeoRAG GitHub Secrets provisioner"
  c_blu "Repo: $(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || echo '<not in a repo>')"
  echo

  require_gh

  c_blu ""
  c_blu "─── Repo-level secrets ───"

  if [ -f "$CI_KEY" ]; then
    c_blu ""
    c_blu "→ SOPS_AGE_PRIVATE_KEY (from ${CI_KEY})"
    read -rp "  Push ${CI_KEY} to repo secret SOPS_AGE_PRIVATE_KEY? [y/N] " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
      set_secret_from_file "SOPS_AGE_PRIVATE_KEY" "" "$CI_KEY"
    else
      c_yel "  ⊝ skipped"
    fi
  else
    set_secret "SOPS_AGE_PRIVATE_KEY" "" \
      "CI age private key (full AGE-SECRET-KEY-1... contents). Run bootstrap-secrets.sh first if missing."
  fi

  set_secret "STAGING_URL" "" \
    "Public URL of staging FastAPI, e.g. https://staging.your-domain.example.com:8000"

  set_secret "STAGING_BASE_URL" "" \
    "Public base URL of staging (Laravel side), e.g. https://staging.your-domain.example.com"

  set_secret "PRODUCTION_BASE_URL" "" \
    "Public base URL of production, e.g. https://your-domain.example.com"

  for ENV in dev staging production; do
    UPPER=$(echo "$ENV" | tr '[:lower:]' '[:upper:]')
    c_blu ""
    c_blu "─── Environment: ${ENV} ───"

    set_secret "${UPPER}_SSH_HOST" "$ENV" \
      "Hostname or IP of the ${ENV} deploy target (leave blank if not provisioned)"

    set_secret "${UPPER}_SSH_USER" "$ENV" \
      "SSH username on the ${ENV} host (e.g. georag-deploy)"

    set_secret "${UPPER}_SSH_KEY" "$ENV" \
      "SSH private key (paste full ED25519 PEM contents incl. BEGIN/END lines)"
  done

  c_grn ""
  c_grn "✓ Secrets provisioning complete."
  c_grn "  Next: bash scripts/operator/preflight.sh"
}

main "$@"
