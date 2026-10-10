<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * silver.exports.download_url: varchar(1000) -> text.
 *
 * Production signs export links with ECS task-role session credentials, so a
 * presigned URL carries an X-Amz-Security-Token that is routinely long enough
 * to take it past 1,000 characters. GenerateExportJob stored the URL in this
 * column, the UPDATE failed with SQLSTATE 22001, and every export on AWS ended
 * 'failed' after the file had been generated and uploaded.
 *
 * The application no longer stores a download URL at all — ExportController
 * mints one from `minio_path` on every show/download — so this is not what
 * fixes the export. It is the safety net: a worker still running the previous
 * release during the rollout would otherwise keep failing on the old width, and
 * a column that exists should not be able to reject the value it was made for.
 * Dropping `download_url` and `download_url_expires_at` is a follow-up for
 * once no such worker remains.
 *
 * varchar(n) -> text is binary-coercible: PostgreSQL changes the type without
 * rewriting the table, and the ACCESS EXCLUSIVE lock it needs is held only for
 * the catalog update. lock_timeout stops the statement queueing behind a long
 * transaction and, in turn, queueing every export read behind itself.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            // The SQLite fast suite does not enforce varchar lengths.
            return;
        }

        DB::statement("SET LOCAL lock_timeout = '5s'");
        DB::statement('ALTER TABLE silver.exports ALTER COLUMN download_url TYPE text');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // A value that no longer fits is a presigned URL nothing reads any more
        // (links are minted per request). Clearing it lets the rollback
        // succeed; truncating it would leave a URL that points nowhere.
        DB::statement('UPDATE silver.exports SET download_url = NULL WHERE length(download_url) > 1000');

        DB::statement("SET LOCAL lock_timeout = '5s'");
        DB::statement('ALTER TABLE silver.exports ALTER COLUMN download_url TYPE varchar(1000)');
    }
};
