<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Let a SURFACE sample into silver.geochemistry.
 *
 * The table was created for down-hole geochemistry, so three columns are
 * NOT NULL: `collar_id` (FK to silver.collars), `from_depth` and `to_depth`.
 * Every one of those is meaningless for a soil, stream-sediment or rock-chip
 * sample — there is no drill hole and no interval, only a location.
 *
 * 2026-04-22 already extended the table for exactly this shape: it added
 * `geom geometry(Point,4326)`, `sample_id`, `sample_type` (whose CHECK
 * explicitly lists 'soil', 'stream_sediment', 'rock_chip', 'grab', 'channel',
 * 'till'), `assay_element_codes` and `assay_values_ppm`. Half of that
 * vocabulary describes samples that CANNOT have a collar_id, so the columns
 * and the constraints have contradicted each other since that day. This
 * migration finishes the job.
 *
 * The trigger: a real delivery carried all_historical_soils_clean.DAT — 854
 * soil samples with easting/northing and Au/Ag/As/Cu/Pb/Zn assays. With
 * collar_id NOT NULL there is nowhere for them to go, and they would land as
 * an untyped attribute table instead of geochemistry the map can draw.
 *
 * WHAT THIS DOES NOT DO: it does not make the drill path laxer. A down-hole
 * assay still carries its collar and its interval, and the partial CHECK
 * below enforces that — depths must travel as a pair, and a row that has one
 * without the other is a bug, not a surface sample.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            // Everything below is Postgres DDL — schema-qualified ALTERs and
            // NOT VALID constraints, neither of which SQLite has. The SQLite
            // suite has no silver.geochemistry to relax either.
            return;
        }

        if (! $this->tableExists('silver', 'geochemistry')) {
            // The test database provisions silver tables selectively; a
            // missing table here means this environment has no geochemistry
            // to relax, not that the migration failed.
            return;
        }

        DB::statement('ALTER TABLE silver.geochemistry ALTER COLUMN collar_id DROP NOT NULL;');
        DB::statement('ALTER TABLE silver.geochemistry ALTER COLUMN from_depth DROP NOT NULL;');
        DB::statement('ALTER TABLE silver.geochemistry ALTER COLUMN to_depth DROP NOT NULL;');

        // Depths travel as a pair or not at all. Dropping NOT NULL on both
        // would otherwise permit a row with a from_depth and no to_depth,
        // which is an interval with no end — not a surface sample, just a
        // broken one. NOT VALID so the statement does not rewrite an existing
        // table; every row already present satisfies it, because both columns
        // were NOT NULL until a moment ago.
        DB::statement('
            ALTER TABLE silver.geochemistry
            ADD CONSTRAINT geochemistry_depth_pair_check
            CHECK (
                (from_depth IS NULL AND to_depth IS NULL)
                OR (from_depth IS NOT NULL AND to_depth IS NOT NULL)
            ) NOT VALID;
        ');
        DB::statement('ALTER TABLE silver.geochemistry VALIDATE CONSTRAINT geochemistry_depth_pair_check;');

        // A surface sample is only useful if it can be placed. This says: a
        // row with no collar must carry its own geometry. A down-hole row
        // still inherits position from its collar and is unaffected.
        DB::statement('
            ALTER TABLE silver.geochemistry
            ADD CONSTRAINT geochemistry_locatable_check
            CHECK (collar_id IS NOT NULL OR geom IS NOT NULL) NOT VALID;
        ');
        DB::statement('ALTER TABLE silver.geochemistry VALIDATE CONSTRAINT geochemistry_locatable_check;');

        // Re-ingesting a delivery must UPDATE its samples, not duplicate
        // them, so the surface-sample writer needs an ON CONFLICT target.
        // There was none: the table's only indexes are non-unique.
        //
        // PARTIAL, on sample_id IS NOT NULL. A down-hole assay has no
        // sample_id — it is identified by collar plus interval — so a plain
        // unique index would collide every one of them on (project_id, NULL)
        // under any future NULLS NOT DISTINCT change, and constrains rows
        // this writer never touches. The partial index applies to exactly the
        // rows that have the identity it is asserting.
        DB::statement('CREATE UNIQUE INDEX IF NOT EXISTS uq_geochemistry_project_sample
            ON silver.geochemistry (project_id, sample_id)
            WHERE sample_id IS NOT NULL;');

        DB::statement("COMMENT ON COLUMN silver.geochemistry.collar_id IS
            'Down-hole samples only. NULL for a surface sample (soil, stream sediment, rock chip), which is located by geom instead. Enforced by geochemistry_locatable_check.';");
        DB::statement("COMMENT ON COLUMN silver.geochemistry.from_depth IS
            'Down-hole samples only. NULL for a surface sample; must be NULL or NOT NULL together with to_depth (geochemistry_depth_pair_check).';");
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            // Everything below is Postgres DDL — schema-qualified ALTERs and
            // NOT VALID constraints, neither of which SQLite has. The SQLite
            // suite has no silver.geochemistry to relax either.
            return;
        }

        if (! $this->tableExists('silver', 'geochemistry')) {
            return;
        }

        DB::statement('DROP INDEX IF EXISTS silver.uq_geochemistry_project_sample;');
        DB::statement('ALTER TABLE silver.geochemistry DROP CONSTRAINT IF EXISTS geochemistry_locatable_check;');
        DB::statement('ALTER TABLE silver.geochemistry DROP CONSTRAINT IF EXISTS geochemistry_depth_pair_check;');

        // Deliberately NOT restoring NOT NULL. Any surface sample written
        // while this migration was applied has a NULL collar_id, so the
        // ALTER would fail on real data and take an otherwise-fine rollback
        // down with it. Reversing the constraints is enough to undo the
        // permission this migration grants; the columns stay nullable.
    }

    /**
     * Whether a schema-qualified table exists.
     *
     * NOT `Schema::hasTable('silver.geochemistry')`. Laravel treats the dotted
     * string as a table NAME, not as schema.table, so on Postgres it looks for
     * a table literally called "silver.geochemistry" in the search path, finds
     * nothing, and this migration would silently no-op in production while
     * passing every test. Every other migration in this repo that guards a
     * silver table queries information_schema directly; this matches them.
     */
    private function tableExists(string $schema, string $table): bool
    {
        return DB::table('information_schema.tables')
            ->where('table_schema', $schema)
            ->where('table_name', $table)
            ->exists();
    }
};
