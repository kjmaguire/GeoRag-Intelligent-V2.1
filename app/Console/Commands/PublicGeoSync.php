<?php

declare(strict_types=1);

namespace App\Console\Commands;

use App\Services\PublicGeoSyncTrigger;
use App\Services\PublicGeoSyncTriggerException;
use Illuminate\Console\Command;

/**
 * Enqueue the public_geo_sync Hatchet workflow from the CLI.
 *
 *   php artisan public-geo:sync
 *   php artisan public-geo:sync --jurisdiction=CA-SK
 *   php artisan public-geo:sync --jurisdiction=CA-SK --max-features=50
 *
 * Same path as the admin "Sync now" button (PublicGeoSyncTrigger → FastAPI
 * `POST /internal/v1/public-geo/sync/trigger` → Hatchet `aio_run_no_wait`),
 * so in production it is an `aws ecs execute-command` into the laravel
 * container rather than a hand-built `run-task` override. Returns as soon as
 * the run is queued and prints its id; the run itself takes hours. It fails
 * the Hatchet run (not this command) if every feed fetches nothing.
 *
 * `--max-features` caps each feed — the smoke test for a mapper change
 * against the live services without pulling half a million rows.
 *
 * This is an on-demand trigger, not a schedule: the weekly cadence is the
 * workflow's own Hatchet cron (CLAUDE.md hard rule 7 — no Laravel scheduler).
 */
class PublicGeoSync extends Command
{
    protected $signature = 'public-geo:sync
                            {--jurisdiction=* : Restrict to these jurisdiction codes (e.g. CA-SK). Repeatable; omit for all.}
                            {--max-features= : Cap features per feed (smoke test).}';

    protected $description = 'Queue a public_geo_sync Hatchet run (refresh public_geo.* from the provincial surveys).';

    public function handle(PublicGeoSyncTrigger $trigger): int
    {
        /** @var list<string> $codes */
        $codes = array_values(array_unique(array_map(
            static fn (mixed $c): string => strtoupper(trim((string) $c)),
            (array) $this->option('jurisdiction'),
        )));

        foreach ($codes as $code) {
            if (preg_match('/^[A-Z]{2}-[A-Z]{2,10}$/', $code) !== 1) {
                $this->error("Invalid jurisdiction code: {$code} (expected e.g. CA-SK)");

                return self::INVALID;
            }
        }

        $maxOption = $this->option('max-features');
        $maxFeatures = null;
        if ($maxOption !== null && $maxOption !== '') {
            if (! ctype_digit((string) $maxOption) || (int) $maxOption < 1) {
                $this->error('--max-features must be a positive integer');

                return self::INVALID;
            }
            $maxFeatures = (int) $maxOption;
        }

        try {
            $result = $trigger->trigger(
                $codes === [] ? null : $codes,
                null,
                'cli:'.(get_current_user() ?: 'unknown'),
                $maxFeatures,
            );
        } catch (PublicGeoSyncTriggerException $exc) {
            $this->error($exc->getMessage());

            return self::FAILURE;
        }

        $this->info('Queued public_geo_sync run '.$result['workflow_run_id']);
        $this->line(sprintf(
            '  jurisdictions: %s · feeds: %d',
            $result['jurisdiction_codes'] === null ? 'all' : implode(', ', $result['jurisdiction_codes']),
            $result['feeds'],
        ));
        $this->line('  A run arriving while another is in flight is cancelled by Hatchet (single-flight).');

        return self::SUCCESS;
    }
}
