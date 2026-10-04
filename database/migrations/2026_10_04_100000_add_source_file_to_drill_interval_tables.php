<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Inline source-file lineage on the tables ingest_tabular replaces per hole.
 *
 * Why: ingest_tabular._write_intervals used to DELETE every row of every
 * hole a file mentioned before inserting, so a second upload covering the
 * same hole (Au.csv then Cu.csv, or a 3-row correction file) silently
 * deleted the first upload's rows, and which one survived depended on which
 * ZIP member's run committed last. With these columns the replace is scoped
 * to rows written by the SAME logical source file, the way
 * ingest_spatial._replace_previous_upload scopes silver.spatial_features.
 *
 *   source_file         the logical file name, WITHOUT the upload timestamp
 *                       prefix. This is the replace key: a corrected
 *                       re-upload of "lith.csv" has a different content hash
 *                       but must still replace the earlier "lith.csv".
 *   source_file_sha256  the content hash of the uploaded bytes, for lineage
 *                       (which exact bronze object wrote the row).
 *
 * Both nullable. Rows written before this migration carry NULL; ingest_tabular
 * keeps replacing NULL-source rows of a re-uploaded hole (it cannot tell which
 * upload wrote them) and says so in the run's warning rather than leaving them
 * to double. No backfill: inventing a source would be the guess the column
 * exists to avoid.
 *
 * Tables: every table _INTERVAL_TABLES names, plus silver.assays_v2, which a
 * sample upload replaces alongside silver.samples.
 *
 * Indexes (collar_id, source_file) are built CONCURRENTLY, hence
 * $withinTransaction; an INVALID leftover of the same name is dropped first.
 * pgsql only.
 */
return new class extends Migration
{
    public $withinTransaction = false;

    /**
     * index name => qualified table.
     *
     * @var array<string, string>
     */
    private const TABLES = [
        'idx_surveys_collar_source_file' => 'silver.surveys',
        'idx_lithology_logs_collar_source_file' => 'silver.lithology_logs',
        'idx_samples_collar_source_file' => 'silver.samples',
        'idx_structure_collar_source_file' => 'silver.structure',
        'idx_alteration_collar_source_file' => 'silver.alteration',
        'idx_mineralization_collar_source_file' => 'silver.mineralization',
        'idx_assays_v2_collar_source_file' => 'silver.assays_v2',
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (self::TABLES as $index => $table) {
            if (! $this->tableExists($table)) {
                continue;
            }

            DB::statement("ALTER TABLE {$table} ADD COLUMN IF NOT EXISTS source_file text");
            DB::statement("ALTER TABLE {$table} ADD COLUMN IF NOT EXISTS source_file_sha256 varchar(64)");
            DB::statement(
                "COMMENT ON COLUMN {$table}.source_file IS 'Logical name of the uploaded file that wrote this row (upload timestamp prefix stripped). The replace key for ingest_tabular re-uploads. NULL = written before 2026-10-04.'",
            );
            DB::statement(
                "COMMENT ON COLUMN {$table}.source_file_sha256 IS 'SHA-256 of the uploaded file bytes that wrote this row (lineage). NULL = written before 2026-10-04.'",
            );

            $schema = explode('.', $table, 2)[0];
            $this->dropIfInvalid($schema, $index);
            DB::statement("CREATE INDEX CONCURRENTLY IF NOT EXISTS {$index} ON {$table} (collar_id, source_file)");
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (array_reverse(self::TABLES, true) as $index => $table) {
            if (! $this->tableExists($table)) {
                continue;
            }

            $schema = explode('.', $table, 2)[0];
            DB::statement("DROP INDEX CONCURRENTLY IF EXISTS {$schema}.{$index}");
            DB::statement("ALTER TABLE {$table} DROP COLUMN IF EXISTS source_file_sha256");
            DB::statement("ALTER TABLE {$table} DROP COLUMN IF EXISTS source_file");
        }
    }

    private function tableExists(string $qualified): bool
    {
        return DB::selectOne('SELECT to_regclass(?) IS NOT NULL AS present', [$qualified])->present;
    }

    private function dropIfInvalid(string $schema, string $index): void
    {
        $invalid = DB::selectOne(
            'SELECT NOT i.indisvalid AS invalid
               FROM pg_index i
               JOIN pg_class c ON c.oid = i.indexrelid
               JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = ? AND c.relname = ?',
            [$schema, $index],
        );

        if ($invalid !== null && $invalid->invalid) {
            DB::statement("DROP INDEX CONCURRENTLY IF EXISTS {$schema}.{$index}");
        }
    }
};
