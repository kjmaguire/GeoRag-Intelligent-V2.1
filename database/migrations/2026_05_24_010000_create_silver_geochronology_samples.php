<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * CC-03 Item 3 — silver.geochronology_samples (radiometric age determinations).
 *
 * Per the cc-03 spec field list (verified absent 2026-05-23):
 *   sample_id            text NOT NULL — lab/publication sample ID
 *   workspace_id         uuid NOT NULL — RLS tenancy
 *   project_id           uuid          — nullable for academic / govt records
 *   rock_type            text          — host lithology free-text
 *   isotopic_system      text CHECK    — U-Pb / Pb-Pb / Ar-Ar / K-Ar / Re-Os /
 *                                        Rb-Sr / Sm-Nd / Lu-Hf / other
 *   mineral_dated        text          — zircon, titanite, monazite, apatite,
 *                                        allanite, biotite, etc. free-text
 *                                        because mineral nomenclature varies
 *                                        (e.g. "magmatic zircon" vs "zircon").
 *   age_ma               numeric       — age in millions of years
 *   age_uncertainty_ma   numeric       — store as 2σ + uncertainty_kind flag
 *   uncertainty_kind     text CHECK    — '2sigma' / '1sigma' / 'unknown'
 *   analytical_method    text          — LA-ICP-MS, SHRIMP, TIMS, etc.
 *   laboratory           text          — Pacific Centre for Isotopic Geochem,
 *                                        ANU SHRIMP, etc.
 *   publication_ref      text          — DOI / report ID / citation
 *   geom                 geometry(Point, 4326)
 *   spatial_uncertainty_m, crs_confidence, georef_method — CC-01 Item 2
 *     spatial-uncertainty columns (same pattern as silver.collars).
 *   created_at / updated_at
 *
 * Workspace tenancy is mandatory (matches every other silver.* table).
 *
 * Indexes:
 *   - GIST on geom for spatial filter (age-vs-location maps)
 *   - btree on workspace_id + project_id (scoping path)
 *   - btree on (isotopic_system, age_ma) — PCA-style biplot bucketing
 *
 * SQLite — gated on Postgres (PostGIS geometry + CHECKs use varchar enums).
 *
 * NB: this is metadata-only; the analytical surface (PCA biplots,
 * age-vs-location maps) is its own follow-on once Zina is an early adopter.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.geochronology_samples (
                sample_pk              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id           uuid NOT NULL,
                project_id             uuid,
                sample_id              text NOT NULL,
                rock_type              text,
                isotopic_system        varchar(16) NOT NULL,
                mineral_dated          text,
                age_ma                 numeric,
                age_uncertainty_ma     numeric,
                uncertainty_kind       varchar(16),
                analytical_method      text,
                laboratory             text,
                publication_ref        text,
                geom                   geometry(Point, 4326),
                spatial_uncertainty_m  real,
                crs_confidence         real,
                georef_method          varchar(16),
                created_at             timestamptz NOT NULL DEFAULT now(),
                updated_at             timestamptz NOT NULL DEFAULT now(),

                CONSTRAINT chk_geochron_isotopic_system
                    CHECK (isotopic_system IN ('U-Pb', 'Pb-Pb', 'Ar-Ar', 'K-Ar',
                                                'Re-Os', 'Rb-Sr', 'Sm-Nd', 'Lu-Hf',
                                                'other')),
                CONSTRAINT chk_geochron_uncertainty_kind
                    CHECK (uncertainty_kind IS NULL
                           OR uncertainty_kind IN ('2sigma', '1sigma', 'unknown')),
                CONSTRAINT chk_geochron_age_nonneg
                    CHECK (age_ma IS NULL OR age_ma >= 0),
                CONSTRAINT chk_geochron_age_unc_nonneg
                    CHECK (age_uncertainty_ma IS NULL OR age_uncertainty_ma >= 0),
                CONSTRAINT chk_geochron_crs_confidence
                    CHECK (crs_confidence IS NULL
                           OR (crs_confidence >= 0 AND crs_confidence <= 1)),
                CONSTRAINT chk_geochron_georef_method
                    CHECK (georef_method IS NULL
                           OR georef_method IN ('declared', 'detected', 'assumed',
                                                'manual', 'survey')),
                CONSTRAINT chk_geochron_uncertainty_nonneg
                    CHECK (spatial_uncertainty_m IS NULL OR spatial_uncertainty_m >= 0),

                CONSTRAINT fk_geochron_workspace
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE,
                CONSTRAINT fk_geochron_project
                    FOREIGN KEY (project_id)
                    REFERENCES silver.projects (project_id)
                    ON DELETE SET NULL,

                CONSTRAINT uq_geochron_workspace_sample
                    UNIQUE (workspace_id, sample_id, isotopic_system)
            )
        SQL);

        DB::statement("COMMENT ON TABLE silver.geochronology_samples IS
            'CC-03 Item 3 — radiometric age determinations. One row per (sample, isotopic system). project_id nullable for academic/govt records. age_uncertainty stored alongside uncertainty_kind so 1σ vs 2σ stays explicit.'");
        DB::statement("COMMENT ON COLUMN silver.geochronology_samples.isotopic_system IS
            'U-Pb / Pb-Pb / Ar-Ar / K-Ar / Re-Os / Rb-Sr / Sm-Nd / Lu-Hf / other';");
        DB::statement("COMMENT ON COLUMN silver.geochronology_samples.mineral_dated IS
            'Mineral phase dated (zircon, titanite, monazite, apatite, allanite, biotite, etc.). Free-text — nomenclature varies between labs.';");
        DB::statement("COMMENT ON COLUMN silver.geochronology_samples.uncertainty_kind IS
            'Whether age_uncertainty_ma is stored at 1σ, 2σ, or unknown. 2σ is preferred per the cc-03 spec.';");
        DB::statement("COMMENT ON COLUMN silver.geochronology_samples.spatial_uncertainty_m IS
            'CC-01 Item 2 — radius of positional uncertainty in metres; NULL = not recorded.';");
        DB::statement("COMMENT ON COLUMN silver.geochronology_samples.crs_confidence IS
            'CC-01 Item 2 — confidence (0-1) that the recorded CRS is correct.';");
        DB::statement("COMMENT ON COLUMN silver.geochronology_samples.georef_method IS
            'CC-01 Item 2 — how the spatial location was assigned. See chk_geochron_georef_method for the vocabulary.';");

        DB::statement('CREATE INDEX IF NOT EXISTS idx_geochron_geom_gist ON silver.geochronology_samples USING gist (geom)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geochron_workspace ON silver.geochronology_samples (workspace_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geochron_project ON silver.geochronology_samples (project_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geochron_system_age ON silver.geochronology_samples (isotopic_system, age_ma)');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS silver.geochronology_samples CASCADE');
    }
};
