#!/usr/bin/env bash
# =============================================================================
# scripts/phase4_sender_register.sh
#
# Phase 4 Step 1 helper — register a new HMAC sender for the
# external_notification flow (or rotate an existing sender's key).
#
# Usage:
#   phase4_sender_register.sh add <source> [description]
#       — generates a 32-byte secret, kid="primary", inserts the row,
#         and prints the secret ONCE so the operator can hand it to
#         the sender. Subsequent reads require operator-side records;
#         we do not store the plaintext anywhere outside the DB.
#
#   phase4_sender_register.sh rotate <source> [description]
#       — generates a new secret, kid="rotN" where N increments per
#         rotation, and chains via rotated_from_id to the prior row.
#         Both keys remain active until the operator runs `disable`
#         on the old one.
#
#   phase4_sender_register.sh list
#       — shows source / secret_kid / created / last_seen / disabled
#         (plaintext NOT shown).
#
#   phase4_sender_register.sh disable <id>
#       — marks one row disabled (kill switch). Idempotent.
#
# Operator note: the secret is shown ONCE on `add` / `rotate`. Lose it
# and the only recovery is rotation.
# =============================================================================

set -euo pipefail

CONTAINER="${POSTGRES_CONTAINER:-georag-postgresql}"
PG_USER="${POSTGRES_USER:-georag}"
PG_DB="${POSTGRES_DB:-georag}"
ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
ENC_KEY=$(grep '^AUDIT_ENCRYPTION_KEY=' "$ENVFILE" | cut -d= -f2- | head -1)

q() {
    docker exec "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc "$1"
}
q_quiet() {
    docker exec "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -q -c "$1" >/dev/null
}

usage() {
    sed -n '4,30p' "$0"
    exit 2
}

cmd="${1:-}"
[ -z "$cmd" ] && usage
[ -z "$ENC_KEY" ] && { echo "AUDIT_ENCRYPTION_KEY not in $ENVFILE" >&2; exit 1; }

set_guc_then() {
    local payload="$1"
    docker exec "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc \
        "SELECT set_config('app.audit_encryption_key', '${ENC_KEY}', false); $payload"
}

case "$cmd" in
    add)
        source="${2:-}"
        desc="${3:-}"
        [ -z "$source" ] && usage
        secret=$(openssl rand -hex 32)
        new_id=$(set_guc_then \
            "SELECT usage.register_external_notification_sender(
                '${source}', 'primary', '${secret}',
                '${desc}', NULL);" | tail -1 | tr -d ' ')
        echo "OK — sender registered"
        echo "  source     : ${source}"
        echo "  secret_kid : primary"
        echo "  id         : ${new_id}"
        echo "  secret     : ${secret}"
        echo
        echo "Save this secret — it will not be shown again."
        ;;

    rotate)
        source="${2:-}"
        desc="${3:-}"
        [ -z "$source" ] && usage
        # Find current highest rot suffix; default kid="rot1" if none.
        prior=$(q "
            SELECT id::text || '|' || secret_kid
              FROM usage.external_notification_senders
             WHERE source = '${source}' AND disabled_at IS NULL
             ORDER BY created_at DESC LIMIT 1;")
        if [ -z "$prior" ]; then
            echo "no active key for source=${source}; use 'add' first" >&2
            exit 1
        fi
        prior_id="${prior%%|*}"
        prior_kid="${prior##*|}"
        # Bump the rot suffix (primary → rot1 → rot2 → …).
        if [ "$prior_kid" = "primary" ]; then
            new_kid="rot1"
        else
            n="${prior_kid#rot}"
            new_kid="rot$((n + 1))"
        fi
        secret=$(openssl rand -hex 32)
        new_id=$(set_guc_then \
            "SELECT usage.register_external_notification_sender(
                '${source}', '${new_kid}', '${secret}',
                '${desc}', '${prior_id}'::uuid);" | tail -1 | tr -d ' ')
        echo "OK — sender rotated"
        echo "  source     : ${source}"
        echo "  prior_kid  : ${prior_kid}  (id=${prior_id}, still active)"
        echo "  new_kid    : ${new_kid}"
        echo "  id         : ${new_id}"
        echo "  secret     : ${secret}"
        echo
        echo "Both keys verify. Disable the old one with:"
        echo "  $0 disable ${prior_id}"
        ;;

    list)
        q "
            SELECT source || E'\t' || secret_kid || E'\t' ||
                   COALESCE(rotated_from_id::text, '-') || E'\t' ||
                   created_at::text || E'\t' ||
                   COALESCE(last_seen_at::text, 'never') || E'\t' ||
                   COALESCE(disabled_at::text, 'active') || E'\t' ||
                   id::text
              FROM usage.external_notification_senders
             ORDER BY source, created_at DESC;" \
        | column -t -s $'\t' -N 'source,kid,rotated_from,created,last_seen,status,id'
        ;;

    disable)
        id="${2:-}"
        [ -z "$id" ] && usage
        q_quiet "
            UPDATE usage.external_notification_senders
               SET disabled_at = clock_timestamp()
             WHERE id = '${id}'::uuid AND disabled_at IS NULL;"
        echo "OK — disabled ${id}"
        ;;

    *)
        usage
        ;;
esac
