<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Phase 2.1 — commodity + status crosswalk lookup tables.
 *
 * These lookups let Silver-tier ingestion map each jurisdiction's raw
 * attribute strings to the canonical vocabulary enforced by CHECK
 * constraints on pg_mine / pg_mineral_occurrence / pg_drillhole_collar /
 * pg_resource_potential_zone.
 *
 * Unmapped values are the contract for "log and set to 'unknown'" (plan
 * §04c) — they are NOT a hard error.
 *
 * Ownership:
 *   commodity_aliases  — shared across all jurisdictions. Keyed on the
 *                        lowercased alias. Use case-folding at the
 *                        application layer; a UNIQUE index enforces
 *                        one row per lowercased alias.
 *
 *   status_aliases     — jurisdiction- and canonical-type-scoped. The
 *                        same raw string "Producer" can mean different
 *                        canonical values depending on whether it came
 *                        from a Mine Locations layer vs. an SMDI layer.
 *
 * Seed data for Saskatchewan lands in
 * database/seeders/PublicGeoscience/CommodityAliasesSeeder.php and
 * database/seeders/PublicGeoscience/StatusAliasesSeeder.php.
 */
return new class extends Migration
{
    public function up(): void
    {
        $commodityGrouping = "'precious_metals','base_metals','uranium','potash_salt','industrial_materials','gemstones','lithium','ree','coal','other'";

        DB::statement("
            CREATE TABLE IF NOT EXISTS public_geo.commodity_aliases (
                id                   BIGSERIAL    PRIMARY KEY,
                alias                VARCHAR(128) NOT NULL,
                alias_lower          VARCHAR(128) NOT NULL,
                canonical_code       VARCHAR(16)  NOT NULL,
                canonical_name       VARCHAR(64)  NOT NULL,
                commodity_grouping   VARCHAR(32)  NOT NULL
                    CHECK (commodity_grouping IN ({$commodityGrouping})),
                notes                TEXT         NULL,
                created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_commodity_alias_lower UNIQUE (alias_lower)
            )
        ");
        DB::statement('CREATE INDEX IF NOT EXISTS idx_commodity_aliases_canonical ON public_geo.commodity_aliases (canonical_code)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_commodity_aliases_grouping  ON public_geo.commodity_aliases (commodity_grouping)');

        // Status aliases scoped per (jurisdiction, canonical_type).
        $canonicalTypes = "'mine','mineral_occurrence','drillhole_collar','resource_potential_zone'";

        DB::statement("
            CREATE TABLE IF NOT EXISTS public_geo.status_aliases (
                id                   BIGSERIAL    PRIMARY KEY,
                jurisdiction_code    VARCHAR(16)  NOT NULL,
                canonical_type       VARCHAR(32)  NOT NULL
                    CHECK (canonical_type IN ({$canonicalTypes})),
                source_value         VARCHAR(255) NOT NULL,
                source_value_lower   VARCHAR(255) NOT NULL,
                canonical_status     VARCHAR(32)  NOT NULL,
                notes                TEXT         NULL,
                created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_status_alias_scope UNIQUE (jurisdiction_code, canonical_type, source_value_lower),
                CONSTRAINT fk_status_aliases_jurisdiction
                    FOREIGN KEY (jurisdiction_code)
                    REFERENCES public_geo.jurisdictions(jurisdiction_code)
                    ON DELETE CASCADE
            )
        ");
        DB::statement('CREATE INDEX IF NOT EXISTS idx_status_aliases_scope ON public_geo.status_aliases (jurisdiction_code, canonical_type)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_status_aliases_canonical ON public_geo.status_aliases (canonical_status)');
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS public_geo.status_aliases CASCADE');
        DB::statement('DROP TABLE IF EXISTS public_geo.commodity_aliases CASCADE');
    }
};
