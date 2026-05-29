<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * CC-04 — Data-type taxonomy MVP (top-level domains only).
 *
 * Adds three tables to formalise document classification:
 *
 *   1. silver.data_domain — lookup of the 4 top-level domains
 *      (Reports / Geology / Geochemistry / Geophysics) + 1 fallback.
 *
 *   2. silver.data_sub_type — lookup of sub-types per domain. The MVP
 *      doesn't require sub-types but the lookup ships seeded so
 *      future code can reference IDs without a follow-on migration.
 *
 *   3. silver.document_domain_tag — many-to-many between a bronze
 *      source file and its applicable domains. Multi-domain is
 *      mandatory — a single drill program legitimately spans
 *      Geology + Geochemistry + Geophysics simultaneously.
 *      Carries extraction_status so the Silver Review Queue can
 *      filter by "ready for geochem review" independently of
 *      lithology extraction state on the same file.
 *
 * Design decisions baked in (per the kickoff doc auto-decisions):
 *   - Single table with nullable sub_type_id (one place to query).
 *   - Auto-classification at Bronze ingest (see the §04p ingest
 *     hook in src/dagster/.../bronze.py for v2; v1 ships with
 *     manual assignment + an 'unclassified' fallback domain).
 *   - SRQ filters by domain_id via a join (UI-side decision).
 *   - Documents land with at least the 'unclassified' tag; a
 *     reviewer reassigns to a real domain in the Foundry UI.
 *
 * The taxonomy lives in the silver schema so RLS workspace_id
 * tenancy applies to per-document tags. The lookup tables are
 * workspace-agnostic (global vocabulary).
 *
 * SQLite — gated on Postgres.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // ── silver.data_domain ────────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.data_domain (
                id          smallint PRIMARY KEY,
                code        text NOT NULL UNIQUE,
                label       text NOT NULL,
                description text
            )
        SQL);

        DB::statement("COMMENT ON TABLE silver.data_domain IS
            'CC-04 — top-level data-type taxonomy. 4 real domains + 1 unclassified fallback. Workspace-agnostic vocabulary.'");

        // Seeded enum-style — IDs are stable + small so application code
        // can hard-code them when convenient.
        DB::statement(<<<'SQL'
            INSERT INTO silver.data_domain (id, code, label, description) VALUES
              (1, 'reports',      'Reports',
                 'Environmental / Government / Regulatory / Internal exploration reports — primarily textual deliverables.'),
              (2, 'geology',      'Geology',
                 'Mapping (2D, structural, alteration), 3D geological models, logged data from drill core / RC.'),
              (3, 'geochemistry', 'Geochemistry',
                 'Surface sampling (soil / rock chip / stream sediment / till) and subsurface sampling (core / RC / channel).'),
              (4, 'geophysics',   'Geophysics',
                 'Electromagnetic (MT / IP / resistivity / airborne EM), gravity, seismic, radiometric, magnetic, and downhole methods.'),
              (99, 'unclassified', 'Unclassified',
                 'Fallback for documents pending review. A document MUST carry at least this tag (or a real domain) before silver promotion.')
            ON CONFLICT (id) DO NOTHING
        SQL);

        // ── silver.data_sub_type ──────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.data_sub_type (
                id          smallint PRIMARY KEY,
                domain_id   smallint NOT NULL REFERENCES silver.data_domain (id),
                code        text NOT NULL UNIQUE,
                label       text NOT NULL
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS idx_data_sub_type_domain ON silver.data_sub_type (domain_id)');

        // Seed the full sub-type vocabulary from the kickoff doc. Future
        // code can reference these IDs without another migration.
        DB::statement(<<<'SQL'
            INSERT INTO silver.data_sub_type (id, domain_id, code, label) VALUES
              -- Reports (1xx)
              (101, 1, 'environmental_impact_assessment', 'Environmental Impact Assessment'),
              (102, 1, 'community_engagement_report',     'Community Engagement Report'),
              (103, 1, 'indigenous_consultation_report',  'Indigenous Consultation Report'),
              (104, 1, 'assessment_report_filed',         'Assessment Report (Filed)'),
              (105, 1, 'tenure_maintenance_filing',       'Tenure Maintenance Filing'),
              (106, 1, 'ni_43_101_technical_report',      'NI 43-101 Technical Report'),
              (107, 1, 'feasibility_study',               'Feasibility / Pre-Feasibility Study'),
              (108, 1, 'preliminary_economic_assessment', 'Preliminary Economic Assessment'),
              (109, 1, 'historic_data_appraisal',         'Historic Data Appraisal'),
              (110, 1, 'exploration_summary',             'Exploration Summary'),
              (111, 1, 'project_management_report',       'Project Management Report'),
              -- Geology (2xx)
              (201, 2, 'map_surficial',                   '2D Geologic Map — Surficial'),
              (202, 2, 'map_bedrock',                     '2D Geologic Map — Bedrock'),
              (203, 2, 'map_structural',                  'Structural Mapping'),
              (204, 2, 'map_alteration',                  'Alteration Mapping'),
              (205, 2, 'model_3d_leapfrog',               '3D Model — Leapfrog / Micromine Export'),
              (206, 2, 'model_3d_wireframe',              '3D Model — Wireframe Solid'),
              (207, 2, 'model_3d_block',                  '3D Model — Block Model'),
              (208, 2, 'logged_lithology',                'Logged Lithology (Core / RC)'),
              (209, 2, 'logged_structural',               'Structural Logging (Core Angles / Orientations)'),
              (210, 2, 'logged_geotechnical',             'Geotechnical Logging'),
              -- Geochemistry (3xx)
              (301, 3, 'sample_soil',                     'Soil Sample'),
              (302, 3, 'sample_rock_chip',                'Rock Chip / Grab Sample'),
              (303, 3, 'sample_stream_sediment',          'Stream Sediment Sample'),
              (304, 3, 'sample_till',                     'Till / Glacial Sediment Sample'),
              (305, 3, 'sample_drill_core_assay',         'Drill Core Sample + Assay'),
              (306, 3, 'sample_rc_chip',                  'Reverse Circulation (RC) Chip'),
              (307, 3, 'sample_channel',                  'Channel Sample'),
              -- Geophysics (4xx)
              (401, 4, 'geophys_mt',                      'Magnetotellurics (MT)'),
              (402, 4, 'geophys_ip',                      'Induced Polarization (IP)'),
              (403, 4, 'geophys_resistivity',             'Resistivity'),
              (404, 4, 'geophys_airborne_em',             'Airborne EM (TDEM / FDEM)'),
              (405, 4, 'geophys_gravity',                 'Gravity'),
              (406, 4, 'geophys_seismic',                 'Seismic'),
              (407, 4, 'geophys_radiometric',             'Radiometric / Gamma-ray'),
              (408, 4, 'geophys_magnetic',                'Magnetic (Airborne / Ground)'),
              (409, 4, 'geophys_downhole_resistivity',    'Downhole Resistivity'),
              (410, 4, 'geophys_downhole_magnetics',      'Downhole Magnetics'),
              (411, 4, 'geophys_borehole_em',             'Borehole EM')
            ON CONFLICT (id) DO NOTHING
        SQL);

        // ── silver.document_domain_tag ────────────────────────────────────
        // PK includes sub_type_id (with a sentinel 0 for NULLs) so the
        // same document can carry multiple sub-types within one domain
        // (e.g. a drill program's Geology tag spans logged_lithology AND
        // logged_structural). COALESCE in the PK is the standard Postgres
        // workaround for NULLable columns in composite PKs.
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.document_domain_tag (
                document_id         uuid NOT NULL REFERENCES bronze.source_files (id) ON DELETE CASCADE,
                domain_id           smallint NOT NULL REFERENCES silver.data_domain (id),
                sub_type_id         smallint REFERENCES silver.data_sub_type (id),
                workspace_id        uuid NOT NULL,
                assigned_by         text NOT NULL,
                assigned_confidence real,
                extraction_status   text NOT NULL DEFAULT 'pending',
                extraction_run_id   uuid,
                created_at          timestamptz NOT NULL DEFAULT now(),
                updated_at          timestamptz NOT NULL DEFAULT now(),

                CONSTRAINT chk_ddt_assigned_by
                    CHECK (assigned_by IN ('auto', 'user', 'reviewer')),
                CONSTRAINT chk_ddt_assigned_confidence
                    CHECK (assigned_confidence IS NULL
                           OR (assigned_confidence >= 0 AND assigned_confidence <= 1)),
                CONSTRAINT chk_ddt_extraction_status
                    CHECK (extraction_status IN (
                        'pending', 'in_progress', 'extracted',
                        'review_required', 'accepted', 'failed'
                    ))
            )
        SQL);

        // Unique constraint using COALESCE expression so NULL sub_type_id
        // collapses to 0 and is treated as equal across rows.
        DB::statement(<<<'SQL'
            CREATE UNIQUE INDEX IF NOT EXISTS uq_ddt_document_domain_sub_type
              ON silver.document_domain_tag
                 (document_id, domain_id, COALESCE(sub_type_id, 0))
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS idx_ddt_workspace ON silver.document_domain_tag (workspace_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_ddt_workspace_domain ON silver.document_domain_tag (workspace_id, domain_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_ddt_extraction_status ON silver.document_domain_tag (extraction_status) WHERE extraction_status IN (\'pending\', \'review_required\', \'failed\')');

        DB::statement("COMMENT ON TABLE silver.document_domain_tag IS
            'CC-04 — many-to-many between a bronze source file and its applicable domains. Multi-domain is mandatory. extraction_status surfaces in the Silver Review Queue filter.'");
        DB::statement("COMMENT ON COLUMN silver.document_domain_tag.assigned_by IS
            'auto = ingest classifier; user = uploader picked at upload time; reviewer = SRQ moderator overrode auto tag.'");
        DB::statement("COMMENT ON COLUMN silver.document_domain_tag.extraction_status IS
            'Per-domain extraction lifecycle. A drill program may be ''accepted'' for Geology and still ''pending'' for Geochemistry on the same file.'");
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS silver.document_domain_tag CASCADE');
        DB::statement('DROP TABLE IF EXISTS silver.data_sub_type CASCADE');
        DB::statement('DROP TABLE IF EXISTS silver.data_domain CASCADE');
    }
};
