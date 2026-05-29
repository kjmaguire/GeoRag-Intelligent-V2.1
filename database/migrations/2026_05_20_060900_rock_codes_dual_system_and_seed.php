<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Drillhole schema follow-up — rock_codes dual system support.
 *
 * Kyle's call (2026-05-20): support BOTH NRCAN Standard AND GSC
 * Terminology side-by-side so geologists can see equivalent codes
 * across the two systems. The UI's lithology logger will surface
 * both; the silver.lithology.rock_code column references whichever
 * the logger picked.
 *
 * Schema change:
 *   - ADD COLUMN system text (NRCAN | GSC | custom)
 *   - DROP UNIQUE (workspace_id, code)
 *   - ADD UNIQUE (workspace_id, system, code)  — same code can exist
 *     in both systems with different semantics.
 *
 * Seed: a small high-frequency starter set for both systems. The
 * full NRCAN code list runs to several hundred entries; this seed
 * covers the rocks geologists most commonly log in Saskatchewan
 * uranium / Athabasca-basin programs. The lithology ingester will
 * fail gracefully on unknown codes (logs a warning, leaves
 * rock_code NULL) so the catalogue can grow incrementally.
 *
 * The seed targets the platform_ops workspace via a placeholder UUID
 * stamp. Each real workspace gets its own copy on first lithology
 * ingest via the silver.rock_codes UPSERT path in the Dagster
 * bronze_to_silver/lithology.py asset (Step 7).
 *
 * Sources for the seed:
 *   - NRCAN: "Standard codes for rock types" (Natural Resources Canada)
 *   - GSC:   "GSC Lithology terminology" (Geological Survey of Canada)
 *
 * SQLite (test DB) — gated on Postgres.
 */
return new class extends Migration
{
    private const SEED_WORKSPACE_ID = '00000000-0000-0000-0000-000000000001';

    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // Schema change: replace single-code UNIQUE with (system, code).
        DB::statement(<<<'SQL'
            ALTER TABLE silver.rock_codes
              ADD COLUMN IF NOT EXISTS system text NOT NULL DEFAULT 'NRCAN'
                CHECK (system IN ('NRCAN', 'GSC', 'custom'))
        SQL);

        // The old UNIQUE was (workspace_id, code). Drop it and create
        // the dual-system constraint.
        DB::statement('ALTER TABLE silver.rock_codes DROP CONSTRAINT IF EXISTS rock_codes_workspace_id_code_key');
        DB::statement(<<<'SQL'
            CREATE UNIQUE INDEX IF NOT EXISTS silver_rock_codes_workspace_system_code_idx
              ON silver.rock_codes (workspace_id, system, code)
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.rock_codes.system IS
              'Code system: NRCAN (Natural Resources Canada standard), GSC (Geological Survey of Canada terminology), or custom (per-workspace local codes). Same rock can have entries in multiple systems — the UI surfaces all matches.'
        SQL);

        // Seed both systems for the default workspace.
        $nrcan = [
            ['GN', 'Gneiss',                'Foliated metamorphic rock, banded mineralogy'],
            ['QFG', 'Quartz-Feldspar Gneiss', 'Felsic gneiss dominated by quartz + feldspar'],
            ['GR', 'Granite',                'Coarse-grained felsic plutonic'],
            ['MG', 'Migmatite',              'Mixed igneous-metamorphic rock'],
            ['PEG', 'Pegmatite',             'Very coarse-grained intrusive'],
            ['QTZ', 'Quartzite',             'Metamorphosed quartz sandstone'],
            ['SCH', 'Schist',                'Strongly foliated medium-grade metamorphic'],
            ['MAS', 'Sandstone',             'Clastic sedimentary (Athabasca sandstone)'],
            ['SH', 'Shale',                  'Fine-grained clastic sedimentary'],
            ['CGL', 'Conglomerate',          'Coarse clastic sedimentary'],
            ['LIM', 'Limestone',             'Carbonate sedimentary'],
            ['DOL', 'Dolomite',              'Mg-rich carbonate sedimentary'],
            ['MAF', 'Mafic',                 'Generic mafic intrusive (gabbro / diorite)'],
            ['ULM', 'Ultramafic',            'Ol/px-rich intrusive (peridotite / dunite)'],
            ['OVB', 'Overburden',            'Quaternary unconsolidated cover'],
        ];

        $gsc = [
            ['gnss', 'gneiss',              'Foliated metamorphic, GSC terminology'],
            ['qfgn', 'quartzofeldspathic gneiss', 'Quartz + feldspar dominated gneiss'],
            ['gran', 'granite',             'Coarse-grained felsic plutonic'],
            ['mgmt', 'migmatite',           'Partial-melt mixed rock'],
            ['pegm', 'pegmatite',           'Very coarse-grained intrusive'],
            ['qtzt', 'quartzite',           'Metamorphosed quartz sandstone'],
            ['schi', 'schist',              'Strongly foliated metamorphic'],
            ['sast', 'sandstone',           'Clastic sedimentary'],
            ['shal', 'shale',               'Fine-grained clastic'],
            ['cong', 'conglomerate',        'Coarse clastic'],
            ['lmst', 'limestone',           'Carbonate sedimentary'],
            ['dolo', 'dolostone',           'Mg-carbonate sedimentary'],
            ['gabb', 'gabbro',              'Coarse-grained mafic plutonic'],
            ['peri', 'peridotite',          'Ol-rich ultramafic'],
            ['regO', 'regolith',            'Weathered surface cover'],
        ];

        $stmt = 'INSERT INTO silver.rock_codes (workspace_id, system, code, name, description) VALUES (?::uuid, ?, ?, ?, ?) ON CONFLICT DO NOTHING';
        foreach ($nrcan as [$code, $name, $desc]) {
            DB::insert($stmt, [self::SEED_WORKSPACE_ID, 'NRCAN', $code, $name, $desc]);
        }
        foreach ($gsc as [$code, $name, $desc]) {
            DB::insert($stmt, [self::SEED_WORKSPACE_ID, 'GSC', $code, $name, $desc]);
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::delete(
            'DELETE FROM silver.rock_codes WHERE workspace_id = ?::uuid',
            [self::SEED_WORKSPACE_ID],
        );
        DB::statement('DROP INDEX IF EXISTS silver.silver_rock_codes_workspace_system_code_idx');
        DB::statement('ALTER TABLE silver.rock_codes DROP COLUMN IF EXISTS system');
    }
};
