<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Comment-only. Corrects two column comments that describe a scorer which
 * no longer exists.
 *
 * `faithfulness_score` and `context_precision_score` were added on
 * 2026-05-30 with comments attributing them to a "Qwen3-as-judge" scorer and
 * ending "NULL = not yet scored." Their only producer,
 * `score_answer_quality.py`, was removed in 09d1d35 on 2026-07-27. Since then
 * every new row has been NULL and always will be.
 *
 * "NULL = not yet scored" is the dangerous half. It reads as work in
 * progress, so a `WHERE faithfulness_score < 0.5` filter returning zero rows
 * looks like "no low-faithfulness answers" rather than "nothing has been
 * scored for a month". That is the exact inversion an answer-quality
 * dashboard would report to someone deciding whether the system is healthy.
 *
 * The columns are NOT dropped. They were live for roughly two months before
 * the writer was removed, so rows from 2026-05-30 to 2026-07-27 may carry
 * real scores; dropping would destroy the only measurements the system has
 * ever taken. Dropping them once that window is confirmed empty (or archived)
 * is a reasonable follow-up, but it is a data decision, not a cleanup.
 *
 * Answer quality IS measured now, from a different table: `answer_quality_watch`
 * compares refusal rate, guard-fire rate, zero-evidence rate and mean
 * confidence over silver.answer_runs. The comments point there.
 */
return new class extends Migration
{
    /** @var array<string, string> */
    private const COMMENTS = [
        'faithfulness_score' => 'DEPRECATED 2026-07-27. Fraction of answer claims supported by retrieved passages (0.0-1.0). Written by score_answer_quality.py, which was REMOVED in 09d1d35 — no writer exists. Values before 2026-07-27 are real; every row since is NULL and will stay NULL. NULL does NOT mean "not yet scored". For live answer quality see silver.answer_runs and the answer_quality_watch workflow.',
        'context_precision_score' => 'DEPRECATED 2026-07-27. Fraction of retrieved passages that were relevant (0.0-1.0). Written by score_answer_quality.py, which was REMOVED in 09d1d35 — no writer exists. Values before 2026-07-27 are real; every row since is NULL and will stay NULL. NULL does NOT mean "not yet scored". For live answer quality see silver.answer_runs and the answer_quality_watch workflow.',
    ];

    public function up(): void
    {
        // SQLite has no COMMENT ON, and the test DB carries the columns
        // under the bare table name anyway (see the 2026_05_30 migration).
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        foreach (self::COMMENTS as $column => $comment) {
            DB::statement(sprintf(
                'COMMENT ON COLUMN audit.query_audit_log.%s IS %s',
                $column,
                DB::getPdo()->quote($comment),
            ));
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        $restore = [
            'faithfulness_score' => 'Qwen3-as-judge: fraction of answer claims supported by retrieved passages (0.0-1.0). NULL = not yet scored.',
            'context_precision_score' => 'Qwen3-as-judge: fraction of retrieved passages that were relevant (0.0-1.0). NULL = not yet scored.',
        ];

        foreach ($restore as $column => $comment) {
            DB::statement(sprintf(
                'COMMENT ON COLUMN audit.query_audit_log.%s IS %s',
                $column,
                DB::getPdo()->quote($comment),
            ));
        }
    }
};
