<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Phase 2.1 — canonical Public Geoscience entity tables.
 *
 * Four entity types (plan §04a) with a companion `_history` table each.
 *
 *   pg_mine                       ↔ pg_mine_history
 *   pg_mineral_occurrence         ↔ pg_mineral_occurrence_history
 *   pg_drillhole_collar           ↔ pg_drillhole_collar_history
 *   pg_resource_potential_zone    ↔ pg_resource_potential_zone_history
 *
 * Upserts are keyed on (source_id, source_feature_id). Unchanged checksums
 * touch `last_seen_at` only; changed checksums append a history row plus
 * update the live row (plan §05b). Missing records are marked stale via
 * `last_seen_at` drift — not hard-deleted.
 *
 * Enum vocabularies are CHECK-constrained on VARCHAR (not PostgreSQL ENUM
 * types), matching the `silver` schema's convention so alias expansion is
 * a simple seed update rather than ALTER TYPE ADD VALUE.
 *
 * This migration creates structure only. Dagster Bronze/Silver assets that
 * populate these tables land in Phase 2.2 and 2.3.
 */
return new class extends Migration
{
    public function up(): void
    {
        // ── Shared enum strings (documented here, enforced per-column) ──
        $mineStatus = "'producing','past-producer','developed-deposit','prospect','closed','unknown'";
        $occStatus = "'occurrence','showing','prospect','deposit','past-producer','producer','unknown'";
        $coreAvail = "'available','partial','unavailable','unknown'";
        $commodityGrouping = "'precious_metals','base_metals','uranium','potash_salt','industrial_materials','gemstones','lithium','ree','coal','other'";

        // ────────────────────────────────────────────────────────────────
        // pg_mine
        // ────────────────────────────────────────────────────────────────
        DB::statement("
            CREATE TABLE IF NOT EXISTS public_geo.pg_mine (
                id                   UUID         PRIMARY KEY,
                jurisdiction_code    VARCHAR(16)  NOT NULL,
                source_id            VARCHAR(64)  NOT NULL,
                source_feature_id    VARCHAR(128) NOT NULL,
                name                 VARCHAR(512) NULL,
                status               VARCHAR(32)  NOT NULL DEFAULT 'unknown'
                    CHECK (status IN ({$mineStatus})),
                commodities          TEXT[]       NOT NULL DEFAULT '{}',
                commodity_grouping   VARCHAR(32)  NULL
                    CHECK (commodity_grouping IS NULL OR commodity_grouping IN ({$commodityGrouping})),
                operator             VARCHAR(512) NULL,
                source_crs           INT          NOT NULL,
                source_geom_wkt      TEXT         NULL,
                source_url           TEXT         NULL,
                source_attributes    JSONB        NOT NULL DEFAULT '{}'::jsonb,
                first_seen_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                last_seen_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                checksum             CHAR(64)     NOT NULL,
                created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_pg_mine_source UNIQUE (source_id, source_feature_id),
                CONSTRAINT fk_pg_mine_jurisdiction
                    FOREIGN KEY (jurisdiction_code)
                    REFERENCES public_geo.jurisdictions(jurisdiction_code)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_pg_mine_source
                    FOREIGN KEY (source_id)
                    REFERENCES public_geo.sources(source_id)
                    ON DELETE RESTRICT
            )
        ");
        DB::statement("SELECT AddGeometryColumn('public_geo', 'pg_mine', 'geom', 4326, 'POINT', 2)");
        self::addCommonIndexes('pg_mine');

        // History sibling — append-only snapshots on change (plan §05b).
        DB::statement("
            CREATE TABLE IF NOT EXISTS public_geo.pg_mine_history (
                history_id           BIGSERIAL    PRIMARY KEY,
                id                   UUID         NOT NULL,
                jurisdiction_code    VARCHAR(16)  NOT NULL,
                source_id            VARCHAR(64)  NOT NULL,
                source_feature_id    VARCHAR(128) NOT NULL,
                name                 VARCHAR(512) NULL,
                status               VARCHAR(32)  NOT NULL,
                commodities          TEXT[]       NOT NULL DEFAULT '{}',
                commodity_grouping   VARCHAR(32)  NULL,
                operator             VARCHAR(512) NULL,
                source_crs           INT          NOT NULL,
                source_geom_wkt      TEXT         NULL,
                source_url           TEXT         NULL,
                source_attributes    JSONB        NOT NULL DEFAULT '{}'::jsonb,
                checksum             CHAR(64)     NOT NULL,
                superseded_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        ");
        DB::statement("SELECT AddGeometryColumn('public_geo', 'pg_mine_history', 'geom', 4326, 'POINT', 2)");
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_mine_history_id ON public_geo.pg_mine_history (id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_mine_history_superseded ON public_geo.pg_mine_history (superseded_at DESC)');

        // ────────────────────────────────────────────────────────────────
        // pg_mineral_occurrence
        // ────────────────────────────────────────────────────────────────
        DB::statement("
            CREATE TABLE IF NOT EXISTS public_geo.pg_mineral_occurrence (
                id                     UUID         PRIMARY KEY,
                jurisdiction_code      VARCHAR(16)  NOT NULL,
                source_id              VARCHAR(64)  NOT NULL,
                source_feature_id      VARCHAR(128) NOT NULL,
                smdi_id                VARCHAR(64)  NULL,
                name                   VARCHAR(512) NULL,
                historic_names         TEXT[]       NOT NULL DEFAULT '{}',
                status                 VARCHAR(32)  NOT NULL DEFAULT 'unknown'
                    CHECK (status IN ({$occStatus})),
                primary_commodities    TEXT[]       NOT NULL DEFAULT '{}',
                associated_commodities TEXT[]       NOT NULL DEFAULT '{}',
                commodity_grouping     VARCHAR(32)  NULL
                    CHECK (commodity_grouping IS NULL OR commodity_grouping IN ({$commodityGrouping})),
                discovery_type         VARCHAR(128) NULL,
                production_flag        BOOLEAN      NOT NULL DEFAULT FALSE,
                reserves_resources     TEXT         NULL,
                source_crs             INT          NOT NULL,
                source_geom_wkt        TEXT         NULL,
                source_url             TEXT         NULL,
                source_attributes      JSONB        NOT NULL DEFAULT '{}'::jsonb,
                first_seen_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                last_seen_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                checksum               CHAR(64)     NOT NULL,
                created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_pg_mineral_occurrence_source UNIQUE (source_id, source_feature_id),
                CONSTRAINT fk_pg_mo_jurisdiction
                    FOREIGN KEY (jurisdiction_code)
                    REFERENCES public_geo.jurisdictions(jurisdiction_code)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_pg_mo_source
                    FOREIGN KEY (source_id)
                    REFERENCES public_geo.sources(source_id)
                    ON DELETE RESTRICT
            )
        ");
        DB::statement("SELECT AddGeometryColumn('public_geo', 'pg_mineral_occurrence', 'geom', 4326, 'POINT', 2)");
        self::addCommonIndexes('pg_mineral_occurrence', commodityColumn: 'primary_commodities');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_mineral_occurrence_smdi ON public_geo.pg_mineral_occurrence (smdi_id)');

        DB::statement("
            CREATE TABLE IF NOT EXISTS public_geo.pg_mineral_occurrence_history (
                history_id             BIGSERIAL    PRIMARY KEY,
                id                     UUID         NOT NULL,
                jurisdiction_code      VARCHAR(16)  NOT NULL,
                source_id              VARCHAR(64)  NOT NULL,
                source_feature_id      VARCHAR(128) NOT NULL,
                smdi_id                VARCHAR(64)  NULL,
                name                   VARCHAR(512) NULL,
                historic_names         TEXT[]       NOT NULL DEFAULT '{}',
                status                 VARCHAR(32)  NOT NULL,
                primary_commodities    TEXT[]       NOT NULL DEFAULT '{}',
                associated_commodities TEXT[]       NOT NULL DEFAULT '{}',
                commodity_grouping     VARCHAR(32)  NULL,
                discovery_type         VARCHAR(128) NULL,
                production_flag        BOOLEAN      NOT NULL DEFAULT FALSE,
                reserves_resources     TEXT         NULL,
                source_crs             INT          NOT NULL,
                source_geom_wkt        TEXT         NULL,
                source_url             TEXT         NULL,
                source_attributes      JSONB        NOT NULL DEFAULT '{}'::jsonb,
                checksum               CHAR(64)     NOT NULL,
                superseded_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        ");
        DB::statement("SELECT AddGeometryColumn('public_geo', 'pg_mineral_occurrence_history', 'geom', 4326, 'POINT', 2)");
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_mo_history_id ON public_geo.pg_mineral_occurrence_history (id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_mo_history_superseded ON public_geo.pg_mineral_occurrence_history (superseded_at DESC)');

        // ────────────────────────────────────────────────────────────────
        // pg_drillhole_collar
        // ────────────────────────────────────────────────────────────────
        DB::statement("
            CREATE TABLE IF NOT EXISTS public_geo.pg_drillhole_collar (
                id                     UUID         PRIMARY KEY,
                jurisdiction_code      VARCHAR(16)  NOT NULL,
                source_id              VARCHAR(64)  NOT NULL,
                source_feature_id      VARCHAR(128) NOT NULL,
                drillhole_id           VARCHAR(128) NULL,
                drillhole_name         VARCHAR(512) NULL,
                company                VARCHAR(512) NULL,
                project_name           VARCHAR(512) NULL,
                date_drilled           DATE         NULL,
                drill_type             VARCHAR(128) NULL,
                commodity_of_interest  TEXT[]       NOT NULL DEFAULT '{}',
                total_length_m         NUMERIC(10,2) NULL,
                inclination_deg        NUMERIC(6,2) NULL,
                azimuth_deg            NUMERIC(6,2) NULL,
                collar_elevation_m     NUMERIC(10,2) NULL,
                stratigraphic_depths   JSONB        NOT NULL DEFAULT '{}'::jsonb,
                core_availability      VARCHAR(32)  NOT NULL DEFAULT 'unknown'
                    CHECK (core_availability IN ({$coreAvail})),
                core_storage           VARCHAR(512) NULL,
                disposition            VARCHAR(128) NULL,
                source_crs             INT          NOT NULL,
                source_geom_wkt        TEXT         NULL,
                source_url             TEXT         NULL,
                source_attributes      JSONB        NOT NULL DEFAULT '{}'::jsonb,
                first_seen_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                last_seen_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                checksum               CHAR(64)     NOT NULL,
                created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_pg_drillhole_collar_source UNIQUE (source_id, source_feature_id),
                CONSTRAINT fk_pg_dc_jurisdiction
                    FOREIGN KEY (jurisdiction_code)
                    REFERENCES public_geo.jurisdictions(jurisdiction_code)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_pg_dc_source
                    FOREIGN KEY (source_id)
                    REFERENCES public_geo.sources(source_id)
                    ON DELETE RESTRICT
            )
        ");
        DB::statement("SELECT AddGeometryColumn('public_geo', 'pg_drillhole_collar', 'geom', 4326, 'POINT', 2)");
        // Drillholes don't carry a status enum or commodity_grouping — drop the
        // common-index helper and index the drill-specific columns directly.
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_dc_jurisdiction ON public_geo.pg_drillhole_collar (jurisdiction_code)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_dc_source      ON public_geo.pg_drillhole_collar (source_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_dc_geom        ON public_geo.pg_drillhole_collar USING GIST (geom)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_dc_drillhole_id ON public_geo.pg_drillhole_collar (drillhole_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_dc_commodity   ON public_geo.pg_drillhole_collar USING GIN (commodity_of_interest)');

        DB::statement("
            CREATE TABLE IF NOT EXISTS public_geo.pg_drillhole_collar_history (
                history_id             BIGSERIAL    PRIMARY KEY,
                id                     UUID         NOT NULL,
                jurisdiction_code      VARCHAR(16)  NOT NULL,
                source_id              VARCHAR(64)  NOT NULL,
                source_feature_id      VARCHAR(128) NOT NULL,
                drillhole_id           VARCHAR(128) NULL,
                drillhole_name         VARCHAR(512) NULL,
                company                VARCHAR(512) NULL,
                project_name           VARCHAR(512) NULL,
                date_drilled           DATE         NULL,
                drill_type             VARCHAR(128) NULL,
                commodity_of_interest  TEXT[]       NOT NULL DEFAULT '{}',
                total_length_m         NUMERIC(10,2) NULL,
                inclination_deg        NUMERIC(6,2) NULL,
                azimuth_deg            NUMERIC(6,2) NULL,
                collar_elevation_m     NUMERIC(10,2) NULL,
                stratigraphic_depths   JSONB        NOT NULL DEFAULT '{}'::jsonb,
                core_availability      VARCHAR(32)  NOT NULL DEFAULT 'unknown',
                core_storage           VARCHAR(512) NULL,
                disposition            VARCHAR(128) NULL,
                source_crs             INT          NOT NULL,
                source_geom_wkt        TEXT         NULL,
                source_url             TEXT         NULL,
                source_attributes      JSONB        NOT NULL DEFAULT '{}'::jsonb,
                checksum               CHAR(64)     NOT NULL,
                superseded_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        ");
        DB::statement("SELECT AddGeometryColumn('public_geo', 'pg_drillhole_collar_history', 'geom', 4326, 'POINT', 2)");
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_dc_history_id ON public_geo.pg_drillhole_collar_history (id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_dc_history_superseded ON public_geo.pg_drillhole_collar_history (superseded_at DESC)');

        // ────────────────────────────────────────────────────────────────
        // pg_resource_potential_zone
        // ────────────────────────────────────────────────────────────────
        DB::statement("
            CREATE TABLE IF NOT EXISTS public_geo.pg_resource_potential_zone (
                id                   UUID         PRIMARY KEY,
                jurisdiction_code    VARCHAR(16)  NOT NULL,
                source_id            VARCHAR(64)  NOT NULL,
                source_feature_id    VARCHAR(128) NOT NULL,
                commodity            VARCHAR(64)  NOT NULL,
                commodity_grouping   VARCHAR(32)  NULL
                    CHECK (commodity_grouping IS NULL OR commodity_grouping IN ({$commodityGrouping})),
                potential_rank       SMALLINT     NULL
                    CHECK (potential_rank IS NULL OR potential_rank BETWEEN 1 AND 6),
                methodology_ref      TEXT         NULL,
                source_crs           INT          NOT NULL,
                source_geom_wkt      TEXT         NULL,
                source_url           TEXT         NULL,
                source_attributes    JSONB        NOT NULL DEFAULT '{}'::jsonb,
                first_seen_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                last_seen_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                checksum             CHAR(64)     NOT NULL,
                created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_pg_rpz_source UNIQUE (source_id, source_feature_id),
                CONSTRAINT fk_pg_rpz_jurisdiction
                    FOREIGN KEY (jurisdiction_code)
                    REFERENCES public_geo.jurisdictions(jurisdiction_code)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_pg_rpz_source
                    FOREIGN KEY (source_id)
                    REFERENCES public_geo.sources(source_id)
                    ON DELETE RESTRICT
            )
        ");
        DB::statement("SELECT AddGeometryColumn('public_geo', 'pg_resource_potential_zone', 'geom', 4326, 'MULTIPOLYGON', 2)");
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_rpz_jurisdiction ON public_geo.pg_resource_potential_zone (jurisdiction_code)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_rpz_source ON public_geo.pg_resource_potential_zone (source_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_rpz_commodity ON public_geo.pg_resource_potential_zone (commodity)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_rpz_rank ON public_geo.pg_resource_potential_zone (potential_rank)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_rpz_geom ON public_geo.pg_resource_potential_zone USING GIST (geom)');

        DB::statement("
            CREATE TABLE IF NOT EXISTS public_geo.pg_resource_potential_zone_history (
                history_id           BIGSERIAL    PRIMARY KEY,
                id                   UUID         NOT NULL,
                jurisdiction_code    VARCHAR(16)  NOT NULL,
                source_id            VARCHAR(64)  NOT NULL,
                source_feature_id    VARCHAR(128) NOT NULL,
                commodity            VARCHAR(64)  NOT NULL,
                commodity_grouping   VARCHAR(32)  NULL,
                potential_rank       SMALLINT     NULL,
                methodology_ref      TEXT         NULL,
                source_crs           INT          NOT NULL,
                source_geom_wkt      TEXT         NULL,
                source_url           TEXT         NULL,
                source_attributes    JSONB        NOT NULL DEFAULT '{}'::jsonb,
                checksum             CHAR(64)     NOT NULL,
                superseded_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        ");
        DB::statement("SELECT AddGeometryColumn('public_geo', 'pg_resource_potential_zone_history', 'geom', 4326, 'MULTIPOLYGON', 2)");
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_rpz_history_id ON public_geo.pg_resource_potential_zone_history (id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_rpz_history_superseded ON public_geo.pg_resource_potential_zone_history (superseded_at DESC)');
    }

    public function down(): void
    {
        // History first (no FKs), then live tables.
        DB::statement('DROP TABLE IF EXISTS public_geo.pg_resource_potential_zone_history CASCADE');
        DB::statement('DROP TABLE IF EXISTS public_geo.pg_drillhole_collar_history CASCADE');
        DB::statement('DROP TABLE IF EXISTS public_geo.pg_mineral_occurrence_history CASCADE');
        DB::statement('DROP TABLE IF EXISTS public_geo.pg_mine_history CASCADE');

        DB::statement('DROP TABLE IF EXISTS public_geo.pg_resource_potential_zone CASCADE');
        DB::statement('DROP TABLE IF EXISTS public_geo.pg_drillhole_collar CASCADE');
        DB::statement('DROP TABLE IF EXISTS public_geo.pg_mineral_occurrence CASCADE');
        DB::statement('DROP TABLE IF EXISTS public_geo.pg_mine CASCADE');
    }

    /**
     * Common index set for point-geometry entities that carry a status enum
     * and a commodity array. Covers plan §06a: GIST on geom, B-tree on
     * jurisdiction_code and status, GIN on the commodity array.
     */
    private static function addCommonIndexes(string $table, string $commodityColumn = 'commodities'): void
    {
        $schema = 'public_geo';
        DB::statement("CREATE INDEX IF NOT EXISTS idx_{$table}_jurisdiction ON {$schema}.{$table} (jurisdiction_code)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_{$table}_source       ON {$schema}.{$table} (source_id)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_{$table}_status       ON {$schema}.{$table} (status)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_{$table}_geom         ON {$schema}.{$table} USING GIST (geom)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_{$table}_commodities  ON {$schema}.{$table} USING GIN ({$commodityColumn})");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_{$table}_grouping     ON {$schema}.{$table} (commodity_grouping)");
    }
};
