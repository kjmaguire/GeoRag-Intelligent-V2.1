<?php

declare(strict_types=1);

namespace App\Console\Commands;

use App\Services\Collars\CanonicalHoleIdIndex;
use App\Services\Collars\CollarDuplicateMerger;
use Illuminate\Console\Command;
use Throwable;

/**
 * Merge ghost collars so UNIQUE (project_id, hole_id_canonical) can be built
 * (§04e, SME-approved, Kyle, 2026-09-29).
 *
 * A DRY RUN BY DEFAULT: without --execute the whole merge is performed inside
 * a transaction and rolled back, so the report — collars merged, child rows
 * re-pointed, rows dropped as duplicates of the survivor's — is exactly what
 * --execute would do, and nothing changes.
 *
 * Production (the owner role, direct connection — the same one `migrate` uses):
 *
 *     php artisan collars:merge-duplicates --database=pgsql_migrations
 *     php artisan collars:merge-duplicates --database=pgsql_migrations --execute
 *
 * --execute builds the unique index when it succeeds; so does the next
 * `php artisan migrate`. See App\Services\Collars\CollarDuplicateMerger for
 * what a merge does and deliberately does not do.
 */
final class MergeDuplicateCollars extends Command
{
    protected $signature = 'collars:merge-duplicates
        {--dry-run : Report the merge and roll it back (the default; accepted for explicitness)}
        {--execute : Apply the merge, then build the unique index}
        {--project= : Only merge duplicates within this project_id}
        {--database= : Connection to use (production: pgsql_migrations)}';

    protected $description = 'Merge collars whose hole ids canonicalise to the same key into the oldest one (dry run by default)';

    public function handle(CollarDuplicateMerger $merger, CanonicalHoleIdIndex $index): int
    {
        if ($this->option('dry-run') && $this->option('execute')) {
            $this->error('Choose one of --dry-run and --execute.');

            return self::INVALID;
        }

        $connection = $this->option('database') ?: null;
        $project = $this->option('project') ?: null;
        $dryRun = ! $this->option('execute');

        try {
            $report = $merger->merge(
                is_string($connection) ? $connection : null,
                $dryRun,
                is_string($project) ? $project : null,
            );
        } catch (Throwable $e) {
            $this->error('Merge failed and was rolled back: '.$e->getMessage());

            return self::FAILURE;
        }

        $this->line($dryRun
            ? '<comment>DRY RUN</comment> — performed in a transaction and rolled back. Nothing was changed.'
            : '<info>EXECUTED</info> — committed.');
        $this->newLine();

        foreach ($report['groups'] as $group) {
            $ghosts = implode(', ', array_map(
                static fn (array $g): string => "{$g['hole_id']} ({$g['collar_id']})",
                $group['ghosts'],
            ));
            $this->line(sprintf(
                'project %s  key %s  keep %s (%s, created %s)  merge %s',
                $group['project_id'], $group['canonical'],
                $group['survivor']['hole_id'], $group['survivor']['collar_id'],
                $group['survivor']['created_at'] ?? 'unknown', $ghosts,
            ));
            if ($group['filled'] !== []) {
                $this->line('    filled on the survivor from a ghost: '.implode(', ', $group['filled']));
            }
            foreach ($group['tables'] as $table => $counts) {
                $this->line(sprintf(
                    '    %-48s re-pointed %d%s',
                    $table, $counts['repointed'],
                    $counts['dropped'] > 0 ? "  dropped {$counts['dropped']} (the survivor already holds the same key)" : '',
                ));
            }
        }

        foreach ($report['refused'] as $refused) {
            $this->warn(sprintf(
                'refused project %s key %s: %s',
                $refused['project_id'], $refused['canonical'], $refused['reason'],
            ));
        }

        $totals = $report['totals'];
        $this->newLine();
        $this->line(sprintf(
            '%d group(s); %d ghost collar(s) %s; %d child row(s) re-pointed; %d dropped as duplicates of the survivor\'s.',
            $totals['groups'], $totals['ghosts_deleted'], $dryRun ? 'would be deleted' : 'deleted',
            $totals['rows_repointed'], $totals['rows_dropped_as_duplicates'],
        ));

        if ($dryRun) {
            if ($totals['groups'] > 0) {
                $this->line('Re-run with --execute to apply.');
            }

            return self::SUCCESS;
        }

        $state = $report['index'] ?? $index->ensure(is_string($connection) ? $connection : null);
        $this->line('Unique index '.CanonicalHoleIdIndex::INDEX.': '.$state['status'].' — '.$state['message']);
        $this->line('Gold tables and drill traces are rebuilt by the next promote_silver_to_gold run.');

        return in_array($state['status'], [CanonicalHoleIdIndex::STATUS_CREATED, CanonicalHoleIdIndex::STATUS_EXISTS], true)
            ? self::SUCCESS
            : self::FAILURE;
    }
}
