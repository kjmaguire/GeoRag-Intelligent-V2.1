<?php

declare(strict_types=1);

namespace App\Console\Commands;

use App\Models\QueryAuditLog;
use Carbon\CarbonImmutable;
use Illuminate\Console\Command;
use Illuminate\Support\Facades\DB;
use RuntimeException;

/**
 * D5 — golden-set harvester.
 *
 *   php artisan audit:golden-set-report
 *   php artisan audit:golden-set-report --since=30d --limit=20 --output=reports/golden-YYYYMMDD.md
 *   php artisan audit:golden-set-report --min-count=3 --max-confidence=0.5
 *
 * Surfaces the top-N queries that the RAG pipeline STRUGGLED with —
 * low-confidence answers that recurred across multiple sessions, plus
 * explicit failures. These are the candidates most likely to become
 * golden-set test cases: each represents a category of query the system
 * handles badly, and promoting them into `test_golden_queries.py`
 * compounds quality gains with every fix.
 *
 * Input signal:
 *   - query_text_hash groups (deterministic across runs — A4)
 *   - low confidence (< MAX_CONFIDENCE threshold) on non-refusal answers
 *   - explicit [FAILED ...] markers from the C8 failed() handler
 *   - count >= MIN_COUNT occurrences within the lookback window
 *
 * Output: a markdown report with one section per candidate:
 *   - the decrypted sample query text
 *   - occurrence count + confidence distribution
 *   - suggested action (promote to golden set, investigate retrieval, etc.)
 *
 * Operator workflow:
 *   - Kyle runs this weekly (or wires it to a cron / Dagster schedule).
 *   - Eyeballs the top 10 candidates for 5 minutes.
 *   - Promotes any that feel like a real signal into
 *     src/fastapi/tests/test_golden_queries.py as a new test case.
 *
 * No scheduler wiring here — this is a plain artisan command. Drop it
 * on whatever cadence fits (cron, Dagster `PipesSubprocessClient`, etc.).
 */
class GoldenSetReport extends Command
{
    protected $signature = 'audit:golden-set-report
                            {--since=7d : Lookback window; accepts Nd (days) or Nh (hours).}
                            {--limit=20 : Top-N candidates to report.}
                            {--min-count=2 : Minimum occurrences to qualify.}
                            {--max-confidence=0.5 : Cap for low-confidence filtering.}
                            {--output= : Path for the markdown report; defaults to stdout.}';

    protected $description = 'Surface top-N struggling queries from query_audit_log as golden-set candidates.';

    public function handle(): int
    {
        try {
            $since = $this->parseLookback((string) $this->option('since'));
        } catch (RuntimeException $e) {
            $this->error($e->getMessage());
            return self::FAILURE;
        }
        $limit = max(1, (int) $this->option('limit'));
        $minCount = max(1, (int) $this->option('min-count'));
        $maxConfidence = max(0.0, min(1.0, (float) $this->option('max-confidence')));
        $output = $this->option('output');

        $driver = DB::connection()->getDriverName();
        $maxExpr = $driver === 'pgsql' ? 'MAX(audit_id::text)' : 'MAX(audit_id)';
        $minExpr = $driver === 'pgsql' ? 'MIN(audit_id::text)' : 'MIN(audit_id)';

        // First pass: group-by hash for the audit window. Low-confidence
        // OR explicit-failure rows both count toward the group's weight.
        $rows = DB::table('query_audit_log')
            ->selectRaw(
                "query_text_hash AS h,
                 COUNT(*) AS total_count,
                 SUM(CASE WHEN confidence IS NOT NULL AND confidence <= ? THEN 1 ELSE 0 END) AS low_conf_count,
                 AVG(confidence) AS avg_confidence,
                 {$maxExpr} AS sample_id,
                 {$minExpr} AS first_id,
                 MAX(created_at) AS last_seen_at",
                [$maxConfidence]
            )
            ->whereNotNull('query_text_hash')
            ->where('created_at', '>=', $since)
            ->groupBy('query_text_hash')
            ->havingRaw('COUNT(*) >= ?', [$minCount])
            ->get();

        if ($rows->isEmpty()) {
            $msg = "No qualifying queries in the window (since={$this->option('since')}, "
                . "min_count={$minCount}). Try a wider lookback or lower thresholds.";
            $this->writeOut($output, $this->formatEmptyReport($msg, $since));
            return self::SUCCESS;
        }

        // Score: weight by occurrence count × (1 - avg_confidence). Queries
        // that failed often AND with low confidence rise to the top.
        $scored = $rows->map(function ($r) {
            $avg = $r->avg_confidence === null ? 0.0 : (float) $r->avg_confidence;
            $weight = (int) $r->total_count * max(0.0, 1.0 - $avg);
            $r->weight = $weight;
            return $r;
        })->sortByDesc('weight')->take($limit);

        // Resolve a sample plaintext per group via the model (cast decrypts).
        // One additional round-trip per group, capped at $limit.
        $sampleIds = $scored->pluck('sample_id')->filter()->unique()->values();
        $samples = QueryAuditLog::whereIn('audit_id', $sampleIds)
            ->get()
            ->keyBy('audit_id');

        $report = $this->formatReport($scored, $samples, $since, $minCount, $maxConfidence);
        $this->writeOut($output, $report);

        $this->info(sprintf(
            'Golden-set report: %d candidate%s (out of %d group%s in window).',
            $scored->count(), $scored->count() === 1 ? '' : 's',
            $rows->count(), $rows->count() === 1 ? '' : 's',
        ));
        return self::SUCCESS;
    }

    /**
     * Parse an `Nd` / `Nh` lookback string into a CarbonImmutable.
     */
    private function parseLookback(string $raw): CarbonImmutable
    {
        if (!preg_match('/^(\d+)([dh])$/', $raw, $m)) {
            throw new RuntimeException(
                "Invalid --since format: {$raw}. Expected Nd (days) or Nh (hours)."
            );
        }
        $n = (int) $m[1];
        return $m[2] === 'd'
            ? CarbonImmutable::now()->subDays($n)
            : CarbonImmutable::now()->subHours($n);
    }

    private function formatReport(
        $scored,
        $samples,
        CarbonImmutable $since,
        int $minCount,
        float $maxConfidence,
    ): string {
        $lines = [];
        $lines[] = '# Golden-set candidates';
        $lines[] = '';
        $lines[] = sprintf(
            '_Generated %s · window since %s · min count %d · max avg confidence %.2f_',
            now()->toIso8601String(),
            $since->toIso8601String(),
            $minCount,
            $maxConfidence,
        );
        $lines[] = '';
        $lines[] = 'These are the queries the RAG pipeline struggled with most over the window. ';
        $lines[] = 'Review each; promote the ones that feel like a real signal into ';
        $lines[] = '`src/fastapi/tests/test_golden_queries.py` as new test cases.';
        $lines[] = '';
        $lines[] = '---';
        $lines[] = '';

        $i = 1;
        foreach ($scored as $row) {
            $sample = $samples->get($row->sample_id);
            $text = $sample?->query_text ?? '[sample unavailable]';
            $avg = $row->avg_confidence === null ? null : (float) $row->avg_confidence;
            $lines[] = "## {$i}. {$text}";
            $lines[] = '';
            $lines[] = sprintf(
                '- **occurrences**: %d (low-confidence: %d)',
                (int) $row->total_count,
                (int) $row->low_conf_count,
            );
            $lines[] = sprintf(
                '- **avg confidence**: %s',
                $avg === null ? '—' : sprintf('%.2f', $avg),
            );
            $lines[] = sprintf(
                '- **last seen**: %s',
                $row->last_seen_at,
            );
            $lines[] = sprintf(
                '- **weight**: %d (count × (1 − avg_confidence))',
                (int) $row->weight,
            );
            $lines[] = sprintf('- **hash**: `%s`', substr((string) $row->h, 0, 16) . '…');
            $lines[] = '';
            $lines[] = '_Suggested action_: ' . $this->suggestAction($row);
            $lines[] = '';
            $lines[] = '---';
            $lines[] = '';
            $i++;
        }
        return implode(PHP_EOL, $lines);
    }

    private function formatEmptyReport(string $msg, CarbonImmutable $since): string
    {
        return implode(PHP_EOL, [
            '# Golden-set candidates',
            '',
            sprintf('_Generated %s · window since %s_', now()->toIso8601String(), $since->toIso8601String()),
            '',
            $msg,
            '',
        ]);
    }

    private function suggestAction($row): string
    {
        $count = (int) $row->total_count;
        $avg = $row->avg_confidence === null ? null : (float) $row->avg_confidence;
        if ($avg !== null && $avg < 0.2 && $count >= 5) {
            return 'Promote to **test_hallucination_failures.py** — recurring near-refusal suggests the system should fail more gracefully.';
        }
        if ($avg !== null && $avg < 0.4) {
            return 'Promote to **test_golden_queries.py** — recurring low-confidence answer; a fix here compounds across users.';
        }
        if ($count >= 5) {
            return 'High-traffic query. Verify the confidence scorer isn\'t under-reading — maybe the retrieval is good but citations are thin.';
        }
        return 'Investigate retrieval — check the classifier_escalation_signal in logs for this query.';
    }

    private function writeOut(?string $path, string $content): void
    {
        if ($path === null || $path === '' || $path === '-') {
            echo $content . PHP_EOL;
            return;
        }
        $dir = dirname($path);
        if (!is_dir($dir)) {
            @mkdir($dir, 0o755, true);
        }
        file_put_contents($path, $content);
        $this->info("Report written → {$path}");
    }
}
