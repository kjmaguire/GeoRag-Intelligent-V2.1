<?php

declare(strict_types=1);

namespace App\Console\Commands;

use Illuminate\Console\Command;
use Illuminate\Support\Facades\DB;
use RuntimeException;
use Throwable;

/**
 * Apply the second DDL layer — database/raw/**.sql — to the connected database.
 *
 * GeoRAG has two parallel schema layers. `database/migrations/*.php` is run by
 * CD via laravel-migrate-job and is fully in sync with Azure. `database/raw/`
 * is applied by hand locally and by ci.yml against a throwaway Postgres — and
 * by nothing at all in cd.yml, so on Azure it has never run.
 *
 * That is worse than "some objects are missing". Several `*_for_test_db`
 * migrations create a cut-down mirror of a table with CREATE TABLE IF NOT
 * EXISTS, on the stated assumption that production already has the real one
 * from raw SQL. On a cluster built from migrations alone — every Azure cluster
 * — the mirror runs first and BECOMES the production schema. That is how
 * audit.audit_ledger_verification_runs ended up with 8 of its 14 columns, and
 * why the nightly hash-chain verification has failed on every run since at
 * least 2026-08-11.
 *
 * This command is the missing mechanism: an ordered, explicit manifest applied
 * through the app's own connection, one transaction per file, failing loudly.
 * It is deliberately NOT a glob — ci.yml globs the directory and swallows
 * every error with `|| true`, which is fine for a throwaway database and
 * unacceptable against a real one. A file earns its place in the manifest by
 * being idempotent and re-run-safe.
 *
 * Run `--pretend` first against any database you have not applied to before.
 */
final class ApplyRawSql extends Command
{
    protected $signature = 'db:apply-raw
        {--pretend : List the files that would run, and check their prerequisites, without executing anything}
        {--only= : Apply only files whose path contains this substring}
        {--database= : Connection to apply on (defaults to the app default; CD uses pgsql_migrations)}';

    protected $description = 'Apply the ordered database/raw manifest to the connected database';

    public function handle(): int
    {
        $connection = $this->option('database') ?: config('database.default');
        $manifestPath = database_path('raw/manifest.json');

        if (! is_file($manifestPath)) {
            $this->error("No manifest at {$manifestPath}.");

            return self::FAILURE;
        }

        /** @var array{files: list<array{path: string, requires?: list<string>, note?: string}>} $manifest */
        $manifest = json_decode((string) file_get_contents($manifestPath), true, 512, JSON_THROW_ON_ERROR);

        $only = (string) ($this->option('only') ?? '');
        $pretend = (bool) $this->option('pretend');

        $applied = 0;
        $skipped = 0;

        foreach ($manifest['files'] as $entry) {
            $relative = $entry['path'];

            if ($only !== '' && ! str_contains($relative, $only)) {
                continue;
            }

            $absolute = database_path('raw/'.$relative);
            if (! is_file($absolute)) {
                $this->error("MISSING  {$relative}");

                return self::FAILURE;
            }

            // A file whose subject tables do not exist has nothing to do here.
            // Skipping is reported, never silent: a required table that is
            // absent is itself a finding, and `db:check-raw-parity` is what
            // turns it into a failure.
            $missing = $this->missingPrerequisites($connection, $entry['requires'] ?? []);
            if ($missing !== []) {
                $this->warn("SKIP     {$relative} — absent: ".implode(', ', $missing));
                $skipped++;

                continue;
            }

            if ($pretend) {
                $this->line("WOULD    {$relative}");
                $applied++;

                continue;
            }

            try {
                DB::connection($connection)->unprepared($this->readSql($absolute));
                $this->info("APPLIED  {$relative}");
                $applied++;
            } catch (Throwable $e) {
                $this->error("FAILED   {$relative}");
                $this->error($e->getMessage());

                return self::FAILURE;
            }
        }

        $this->newLine();
        $this->line(sprintf(
            '%s %d file(s) on %s; skipped %d for absent prerequisites.',
            $pretend ? 'Would apply' : 'Applied',
            $applied,
            $connection,
            $skipped,
        ));

        return self::SUCCESS;
    }

    /**
     * Read a raw file, resolving the psql meta-commands it uses.
     *
     * These files are written for psql. PDO is not psql: it has no `\set`, so
     * a `:variable` reference reaches the server as a bind-parameter marker
     * and the statement fails. Only `\set name 'value'` appears across the
     * manifest, so resolving it here is exact rather than a general psql
     * emulation — and any OTHER backslash command is a hard error rather
     * than something quietly dropped.
     */
    private function readSql(string $absolute): string
    {
        $sql = (string) file_get_contents($absolute);
        $vars = [];

        // Single-quoted patterns throughout: these match literal backslashes,
        // and double-quoting them would need four in a row to say one.
        $lines = preg_split('/\R/', $sql) ?: [];
        foreach ($lines as $i => $line) {
            if (! str_starts_with(ltrim($line), '\\')) {
                continue;
            }

            if (preg_match('/^\s*\x5cset\s+(\w+)\s+\x27(.*)\x27\s*$/', $line, $m) === 1) {
                // psql's own escaping: a backslash-quote inside the value.
                $vars[$m[1]] = str_replace(chr(92).chr(39), chr(39), $m[2]);
                $lines[$i] = '';

                continue;
            }

            throw new RuntimeException(
                'Unsupported psql meta-command in '.basename($absolute).': '.trim($line),
            );
        }

        $sql = implode(PHP_EOL, $lines);

        foreach ($vars as $name => $value) {
            // The value already carries its own quoting, as psql's does.
            $sql = str_replace(':'.$name, $value, $sql);
        }

        return $sql;
    }

    /**
     * @param list<string> $requires Fully-qualified relation names.
     *
     * @return list<string>
     */
    private function missingPrerequisites(string $connection, array $requires): array
    {
        $missing = [];

        foreach ($requires as $relation) {
            $exists = DB::connection($connection)
                ->selectOne('SELECT to_regclass(?) IS NOT NULL AS present', [$relation]);

            if (! ($exists->present ?? false)) {
                $missing[] = $relation;
            }
        }

        return $missing;
    }
}
