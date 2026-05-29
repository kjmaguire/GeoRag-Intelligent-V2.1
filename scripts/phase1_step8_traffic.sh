#!/usr/bin/env bash
# =============================================================================
# scripts/phase1_step8_traffic.sh
#
# Phase 1 Step 8 cutover helper — read / set the platform-default
# `ingest_pdf_hatchet_traffic_pct` flag, or flip the master kill switch.
#
# Usage:
#   phase1_step8_traffic.sh get              # show current platform default
#   phase1_step8_traffic.sh set <0..100>     # set platform default
#   phase1_step8_traffic.sh disable          # ingest_pdf_shadow_enabled=false
#   phase1_step8_traffic.sh enable           # ingest_pdf_shadow_enabled=true
#   phase1_step8_traffic.sh streak           # current clean-streak in days
#   phase1_step8_traffic.sh history [N=20]   # last N feature_flag mutations (R-P1-6)
#
# Writes through psql against the running georag-postgresql container as
# the postgres superuser-equivalent role configured in POSTGRES_USER.
# =============================================================================

set -euo pipefail

CONTAINER="${POSTGRES_CONTAINER:-georag-postgresql}"
PG_USER="${POSTGRES_USER:-georag}"
PG_DB="${POSTGRES_DB:-georag}"

q() {
    docker exec "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc "$1"
}
q_quiet() {
    docker exec "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -q -c "$1" >/dev/null
}

cmd="${1:-get}"

case "$cmd" in
    get)
        cur=$(q "
            SELECT COALESCE(int_value::text, '0')
              FROM workspace.feature_flags
             WHERE workspace_id IS NULL
               AND flag_name = 'ingest_pdf_hatchet_traffic_pct';" | tr -d ' ')
        cur=${cur:-0}
        enabled=$(q "
            SELECT COALESCE(bool_value::text, 'true')
              FROM workspace.feature_flags
             WHERE workspace_id IS NULL
               AND flag_name = 'ingest_pdf_shadow_enabled';" | tr -d ' ')
        enabled=${enabled:-true}
        echo "platform.ingest_pdf_hatchet_traffic_pct = ${cur}%"
        echo "platform.ingest_pdf_shadow_enabled      = ${enabled}"
        ;;

    set)
        val="${2:-}"
        if ! [[ "$val" =~ ^[0-9]+$ ]] || [ "$val" -lt 0 ] || [ "$val" -gt 100 ]; then
            echo "usage: $0 set <0..100>" >&2
            exit 2
        fi
        q_quiet "
            INSERT INTO workspace.feature_flags
                (workspace_id, flag_name, int_value, updated_at)
            VALUES (NULL, 'ingest_pdf_hatchet_traffic_pct', ${val}, now())
            ON CONFLICT (workspace_id, flag_name) DO UPDATE
                SET int_value = EXCLUDED.int_value,
                    updated_at = now();"
        echo "OK — platform.ingest_pdf_hatchet_traffic_pct set to ${val}%"
        ;;

    disable)
        q_quiet "
            INSERT INTO workspace.feature_flags
                (workspace_id, flag_name, bool_value, updated_at)
            VALUES (NULL, 'ingest_pdf_shadow_enabled', false, now())
            ON CONFLICT (workspace_id, flag_name) DO UPDATE
                SET bool_value = EXCLUDED.bool_value,
                    updated_at = now();"
        echo "OK — shadow path disabled (kill switch)"
        ;;

    enable)
        q_quiet "
            INSERT INTO workspace.feature_flags
                (workspace_id, flag_name, bool_value, updated_at)
            VALUES (NULL, 'ingest_pdf_shadow_enabled', true, now())
            ON CONFLICT (workspace_id, flag_name) DO UPDATE
                SET bool_value = EXCLUDED.bool_value,
                    updated_at = now();"
        echo "OK — shadow path re-enabled"
        ;;

    streak)
        days=$(q "
            WITH days AS (
                SELECT date_trunc('day', started_at) AS day,
                       bool_or(classification IN ('minor','divergent','fatal','partial'))
                           AS has_non_clean
                FROM silver.shadow_runs
                WHERE workflow_kind = 'ingest_pdf'
                  AND started_at >= now() - interval '30 days'
                GROUP BY 1
            )
            SELECT count(*)
              FROM (SELECT day, has_non_clean,
                           sum(has_non_clean::int) OVER (ORDER BY day DESC) AS bad
                      FROM days) s
             WHERE bad = 0;" | tr -d ' ')
        days=${days:-0}
        echo "clean_streak_days = ${days}"
        ;;

    history)
        n="${2:-20}"
        if ! [[ "$n" =~ ^[0-9]+$ ]] || [ "$n" -lt 1 ] || [ "$n" -gt 1000 ]; then
            echo "usage: $0 history [1..1000]" >&2
            exit 2
        fi
        docker exec "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -c "
            SELECT changed_at,
                   op,
                   flag_name,
                   COALESCE(workspace_id::text, 'platform') AS scope,
                   COALESCE(old_int_value::text,    old_bool_value::text,
                            old_string_value,       'NULL')          AS old_value,
                   COALESCE(new_int_value::text,    new_bool_value::text,
                            new_string_value,       'NULL')          AS new_value,
                   COALESCE(actor_id::text, 'system')                AS actor
              FROM workspace.feature_flag_history
             ORDER BY changed_at DESC
             LIMIT $n;"
        ;;

    *)
        echo "usage: $0 {get|set <pct>|disable|enable|streak|history [N]}" >&2
        exit 2
        ;;
esac
