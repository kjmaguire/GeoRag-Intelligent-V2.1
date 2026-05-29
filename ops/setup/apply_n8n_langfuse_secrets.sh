#!/usr/bin/env bash
# Generate the 8 secrets for the n8n + Langfuse compose overlays and write
# them surgically into the WSL `.env`. Idempotent — only fills BLANK or
# missing values; never overwrites a key that already has content.
#
# Variables managed:
#   n8n      : N8N_DB_PASSWORD, N8N_BASIC_AUTH_PASSWORD, N8N_ENCRYPTION_KEY
#   Langfuse : LANGFUSE_DB_PASSWORD, CLICKHOUSE_PASSWORD,
#              LANGFUSE_NEXTAUTH_SECRET, LANGFUSE_SALT, LANGFUSE_ENCRYPTION_KEY
#
# IMPORTANT: SALT, *_ENCRYPTION_KEY values are NOT regenerable later
# without invalidating every encrypted credential / API key in those
# stores. This script is therefore careful to skip keys that already
# have content, so re-running it never silently rotates them.
set -euo pipefail

ENV=${1:-/home/georag/projects/georag/.env}
[ -f "$ENV" ] || { echo "no .env at $ENV" >&2; exit 1; }

backup="$ENV.pre-n8n-langfuse-bringup.$(date +%Y%m%d-%H%M%S)"
cp "$ENV" "$backup"
echo "Backup: $backup"

# Generators per spec
gen_b64_24() { openssl rand -base64 24; }
gen_b64_32() { openssl rand -base64 32; }
gen_hex_32()  { openssl rand -hex 32; }

# (KEY, generator-fn) pairs. Order = the order we'll print to summary.
declare -a KEYS=(
  N8N_DB_PASSWORD              gen_b64_24
  N8N_BASIC_AUTH_PASSWORD      gen_b64_24
  N8N_ENCRYPTION_KEY           gen_hex_32
  LANGFUSE_DB_PASSWORD         gen_b64_24
  CLICKHOUSE_PASSWORD          gen_b64_24
  LANGFUSE_NEXTAUTH_SECRET     gen_b64_32
  LANGFUSE_SALT                gen_hex_32
  LANGFUSE_ENCRYPTION_KEY      gen_hex_32
)

GENERATED=0
SKIPPED=()
APPENDED=()

for ((i = 0; i < ${#KEYS[@]}; i += 2)); do
  KEY="${KEYS[$i]}"
  FN="${KEYS[$i+1]}"

  # Detect existing value (first match only — never accidentally rotate dup lines)
  EXISTING=$(grep -E "^${KEY}=" "$ENV" 2>/dev/null | head -1 | cut -d= -f2- || true)

  if [ -n "$EXISTING" ]; then
    SKIPPED+=("$KEY")
    continue
  fi

  VALUE=$($FN)

  # Escape sed-special characters for the substitution. Easier to use a
  # python helper than to wrestle with bash + sed quoting on values that
  # may contain /, &, newlines (our base64 values DO have / and +).
  if grep -qE "^${KEY}=$" "$ENV"; then
    # Empty value present — replace it. Use awk for robust replacement.
    awk -v key="$KEY" -v val="$VALUE" '
      $0 ~ "^"key"=$" { print key "=" val; next }
      { print }
    ' "$ENV" > "$ENV.tmp" && mv "$ENV.tmp" "$ENV"
  else
    # No line at all — append.
    echo "${KEY}=${VALUE}" >> "$ENV"
    APPENDED+=("$KEY")
  fi
  GENERATED=$((GENERATED + 1))
done

echo
echo "Generated: $GENERATED keys"
[ ${#APPENDED[@]} -gt 0 ] && echo "Appended (key didn't exist): ${APPENDED[*]}"
[ ${#SKIPPED[@]} -gt 0 ]  && echo "Skipped (already had a value, no rotation): ${SKIPPED[*]}"
echo
echo "Final state — keys + first 6 chars of each value:"
for ((i = 0; i < ${#KEYS[@]}; i += 2)); do
  KEY="${KEYS[$i]}"
  V=$(grep -E "^${KEY}=" "$ENV" | head -1 | cut -d= -f2-)
  printf "  %-32s = %s…\n" "$KEY" "${V:0:6}"
done
