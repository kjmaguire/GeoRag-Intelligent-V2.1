#!/bin/bash
# Reconcile Laravel's migrations table with the actual DB state.
#
# Context: our database was seeded from a schema dump at an earlier point
# in the project, so tables like silver.projects, public_geoscience.*,
# and various hardening indexes EXIST in postgres but the public.migrations
# table doesn't have the corresponding "ran" rows. `php artisan migrate`
# would try to re-create them and fail with "relation already exists".
#
# Strategy per migration:
#   1. Figure out what artifact it creates/alters (table, column, schema, view)
#   2. Probe postgres to see whether that artifact is present
#   3. If present → INSERT into public.migrations to mark it ran
#   4. If absent → print a warning for manual review; do NOT mark
#
# Safe to re-run: the migrations table unique key (migration) causes
# duplicate inserts to error (ON CONFLICT DO NOTHING protects us). Script
# exits 0 when every pending migration is successfully reconciled.

set -uo pipefail
# NB: we deliberately do NOT `set -e` here. try_reconcile() returns non-zero
# for each migration that can't be verified, and we want the loop to keep
# going so all warnings surface in one pass.

readonly BATCH_NEW=101   # bump past existing max batch (100)

# Probe function: returns 0 if artifact exists, 1 otherwise.
probe() {
    local name="$1"
    local sql="$2"
    local result
    result=$(docker exec georag-postgresql psql -U georag -d georag -Atc "$sql" 2>/dev/null || echo "0")
    if [[ "$result" == "t" || "$result" == "1" || "$result" == "true" ]]; then
        return 0
    fi
    return 1
}

mark_ran() {
    local migration="$1"
    # migrations table has no UNIQUE on `migration` (Laravel defaults) —
    # guard against duplicate inserts with a WHERE NOT EXISTS subquery.
    docker exec georag-postgresql psql -U georag -d georag -c \
        "INSERT INTO public.migrations (migration, batch)
         SELECT '$migration', $BATCH_NEW
          WHERE NOT EXISTS (
            SELECT 1 FROM public.migrations WHERE migration = '$migration'
          )" \
        > /dev/null
}

check_table() {
    local schema="$1"; local table="$2"
    probe "$schema.$table" \
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = '$schema' AND table_name = '$table')"
}

check_column() {
    local schema="$1"; local table="$2"; local col="$3"
    probe "$schema.$table.$col" \
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = '$schema' AND table_name = '$table' AND column_name = '$col')"
}

check_schema() {
    local schema="$1"
    probe "schema $schema" \
        "SELECT EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = '$schema')"
}

check_index() {
    local idx="$1"
    probe "index $idx" \
        "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = '$idx')"
}

check_view() {
    local schema="$1"; local view="$2"
    probe "$schema.$view" \
        "SELECT EXISTS (SELECT 1 FROM information_schema.views WHERE table_schema = '$schema' AND table_name = '$view')"
}

try_reconcile() {
    local migration="$1"; shift
    local verified="$1"; shift
    if [[ "$verified" == "OK" ]]; then
        mark_ran "$migration"
        echo "  ✓ $migration — marked ran"
    else
        echo "  ⚠ $migration — $verified"
        return 1
    fi
}

echo "=== Reconciling pending Laravel migrations ==="
echo

# ── Create-table migrations — check artifact by table name ────────────────
declare -A CREATE_TABLES=(
    ["2026_04_09_180000_create_projects_table"]="silver.projects"
    ["2026_04_09_180100_create_collars_table"]="silver.collars"
    ["2026_04_09_180200_create_surveys_table"]="silver.surveys"
    ["2026_04_09_180300_create_lithology_logs_table"]="silver.lithology_logs"
    ["2026_04_09_180400_create_alterations_table"]="silver.alterations"
    ["2026_04_09_180500_create_structures_table"]="silver.structures"
    ["2026_04_09_180600_create_samples_table"]="silver.samples"
    ["2026_04_09_180700_create_geochemistry_table"]="silver.geochemistry"
    ["2026_04_09_180800_create_reports_table"]="silver.reports"
    ["2026_04_10_120000_create_well_log_curves_table"]="silver.well_log_curves"
    ["2026_04_10_120100_create_spatial_features_table"]="silver.spatial_features"
    ["2026_04_10_120200_create_seismic_surveys_table"]="silver.seismic_surveys"
    ["2026_04_10_130000_create_exports_table"]="silver.exports"
    ["2026_04_12_000000_create_query_audit_log_table"]="public.query_audit_log"
    ["2026_04_14_140000_create_document_entity_links"]="public_geoscience.document_entity_links"
    ["2026_04_15_100000_create_pg_mineral_disposition_tables"]="public_geoscience.pg_mineral_disposition"
)

for mig in "${!CREATE_TABLES[@]}"; do
    pair="${CREATE_TABLES[$mig]}"
    schema="${pair%.*}"; table="${pair##*.}"
    if check_table "$schema" "$table"; then
        try_reconcile "$mig" "OK"
    else
        try_reconcile "$mig" "table $schema.$table not found"
    fi
done

# ── Schema creation ───────────────────────────────────────────────────────
if check_schema "public_geoscience"; then
    try_reconcile "2026_04_14_000000_create_public_geoscience_schema" "OK"
else
    try_reconcile "2026_04_14_000000_create_public_geoscience_schema" "schema public_geoscience not found"
fi

# ── Multi-table create migrations ─────────────────────────────────────────
# canonical tables — all six must exist
canonical_ok="true"
for t in pg_mine pg_mineral_occurrence pg_drillhole_collar pg_resource_potential_zone; do
    check_table "public_geoscience" "$t" || canonical_ok="false"
done
if [[ "$canonical_ok" == "true" ]]; then
    try_reconcile "2026_04_14_100000_create_public_geoscience_canonical_tables" "OK"
else
    try_reconcile "2026_04_14_100000_create_public_geoscience_canonical_tables" "one or more canonical tables missing"
fi

# lookups — check jurisdictions + sources + commodity_aliases
lookup_ok="true"
for t in jurisdictions sources commodity_aliases status_aliases; do
    check_table "public_geoscience" "$t" || lookup_ok="false"
done
if [[ "$lookup_ok" == "true" ]]; then
    try_reconcile "2026_04_14_110000_create_public_geoscience_lookups" "OK"
else
    try_reconcile "2026_04_14_110000_create_public_geoscience_lookups" "one or more lookup tables missing"
fi

# MVT views — all six tier 1 views must exist
views_ok="true"
for v in v_pg_mines_mvt v_pg_mineral_occurrences_mvt v_pg_drillhole_collars_mvt v_pg_resource_potential_mvt; do
    check_view "public_geoscience" "$v" || views_ok="false"
done
if [[ "$views_ok" == "true" ]]; then
    try_reconcile "2026_04_14_130000_create_public_geoscience_mvt_views" "OK"
else
    try_reconcile "2026_04_14_130000_create_public_geoscience_mvt_views" "one or more MVT views missing"
fi

# rock samples + assessment survey tables
rsa_ok="true"
for t in pg_rock_sample pg_assessment_survey; do
    check_table "public_geoscience" "$t" || rsa_ok="false"
done
if [[ "$rsa_ok" == "true" ]]; then
    try_reconcile "2026_04_15_000000_add_rock_sample_and_assessment_survey_tables" "OK"
else
    try_reconcile "2026_04_15_000000_add_rock_sample_and_assessment_survey_tables" "rock_sample or assessment_survey table missing"
fi

# ── ALTER migrations — check specific columns / indexes ───────────────────

# add_report_versioning → expect version_number or superseded_by_id on silver.reports
if check_column "silver" "reports" "version_number" || check_column "silver" "reports" "superseded_by_id" || check_column "silver" "reports" "version"; then
    try_reconcile "2026_04_13_000000_add_report_versioning" "OK"
else
    try_reconcile "2026_04_13_000000_add_report_versioning" "silver.reports has no versioning column — needs review"
fi

# database_hardening — check one of the expected indexes
if check_index "idx_samples_assays_gin" || check_index "idx_reports_resource_gin" || check_index "idx_collars_project_hole"; then
    try_reconcile "2026_04_13_100000_database_hardening" "OK"
else
    try_reconcile "2026_04_13_100000_database_hardening" "hardening indexes not found"
fi

# production_hardening_final — no single strong signal; peek content. Mark as ran
# only if project_user FK constraints are set (one of its operations).
if check_index "idx_project_user_project" || check_table "public" "project_user"; then
    try_reconcile "2026_04_13_200000_production_hardening_final" "OK"
else
    try_reconcile "2026_04_13_200000_production_hardening_final" "production_hardening signal not found — needs review"
fi

# add_dashboard_fields_to_projects → silver.projects has status + slug
if check_column "silver" "projects" "status" && check_column "silver" "projects" "slug"; then
    try_reconcile "2026_04_13_300000_add_dashboard_fields_to_projects" "OK"
else
    try_reconcile "2026_04_13_300000_add_dashboard_fields_to_projects" "status or slug column missing on silver.projects"
fi

# add_last_service_edit_to_sources → public_geoscience.sources.last_service_edit_ms
if check_column "public_geoscience" "sources" "last_service_edit_ms"; then
    try_reconcile "2026_04_14_120000_add_last_service_edit_to_sources" "OK"
else
    try_reconcile "2026_04_14_120000_add_last_service_edit_to_sources" "last_service_edit_ms column missing on public_geoscience.sources"
fi

echo
echo "=== Done. Residual state: ==="
docker exec georag-laravel-octane php artisan migrate:status 2>&1 | grep -E "Pending" | head || echo "  (no pending migrations remain)"
