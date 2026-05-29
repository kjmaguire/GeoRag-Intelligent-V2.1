<?php

declare(strict_types=1);

namespace Database\Seeders;

use Illuminate\Database\Seeder;
use Illuminate\Support\Facades\DB;
use Ramsey\Uuid\Uuid;
use Symfony\Component\Yaml\Yaml;

/**
 * Plan §5a — Golden question seed loader.
 *
 * Reads tests/golden_questions/seed_template.yaml (the SME source-of-
 * truth per Q15) and upserts rows into eval.golden_questions by a
 * deterministic UUID v5 derived from each entry's kebab `id`. Re-runs
 * are idempotent: same `id` produces the same question_id, so the
 * upsert updates fields in place rather than duplicating.
 *
 * Q13 (plan_category as first-class column) deferred — currently the
 * plan §5a category lives in context_setup.plan_category JSONB. When
 * the column lands, this seeder writes both for backward compatibility.
 *
 * Q14 (authored_by_user_id) — Kyle MUST set the placeholder before
 * running. Look for the SEEDER_AUTHOR_USER_ID const below.
 *
 * Q15 (SME edit contract): YAML is committed source-of-truth; SMEs
 * PR additions; this seeder re-runs on deploy.
 *
 * Q16 (where eval runs trigger): unrelated to this seeder — see
 * eval_real_rag_nightly.py (Hatchet) + routes/api.php (on-demand).
 *
 * Run with:
 *     php artisan db:seed --class=GoldenQuestionsSeeder
 *
 * Refs:
 *   - docs/architecture/golden_question_seed_loader_design.md
 *   - tests/golden_questions/seed_template.yaml
 *   - migration 2026_05_13_140000_create_eval_schema.php
 */
class GoldenQuestionsSeeder extends Seeder
{
    /**
     * The user ID recorded as `authored_by_user_id` on every seeded row.
     *
     * Kyle: set this to your real user ID before the first run. The
     * value is stable across reruns — it's the audit author, not a
     * per-row contributor.
     */
    private const SEEDER_AUTHOR_USER_ID = 971; // Kyle (admin) — kyle@georag.local

    /**
     * Absolute path resolver for the YAML source.
     */
    private function yamlPath(): string
    {
        return base_path('tests/golden_questions/seed_template.yaml');
    }

    public function run(): void
    {
        $path = $this->yamlPath();

        if (! is_file($path)) {
            $this->command?->error("Golden question seed YAML not found at: {$path}");

            return;
        }

        $data = Yaml::parseFile($path);
        $categories = $data['categories'] ?? [];

        if (! is_array($categories) || $categories === []) {
            $this->command?->warn('YAML contains no categories — nothing to seed.');

            return;
        }

        $inserted = 0;
        $updated = 0;

        foreach ($categories as $entry) {
            $sourceId = $entry['id'] ?? null;
            $questionText = $entry['question'] ?? null;

            if ($sourceId === null || $questionText === null) {
                $this->command?->warn('Skipping entry missing id or question: '.json_encode($entry));

                continue;
            }

            // Deterministic UUID v5 from kebab id. Re-running with the
            // same id never duplicates; renaming an id produces a new
            // row and orphans the old (so historical run_results stay
            // queryable against the old question_id).
            $questionId = Uuid::uuid5(Uuid::NAMESPACE_OID, $sourceId)->toString();

            $contextSetup = [
                'plan_category' => $entry['category'] ?? null,
                'source_id' => $sourceId,
                'notes' => $entry['notes'] ?? null,
            ];

            $row = [
                'question_set' => $entry['question_set'] ?? 'core_chat',
                'question_text' => $questionText,
                'context_setup' => json_encode($contextSetup, JSON_THROW_ON_ERROR),
                'expected_intent_class' => $entry['expected_intent_class'] ?? null,
                'expected_citations' => json_encode($entry['expected_citations'] ?? [], JSON_THROW_ON_ERROR),
                'expected_entities' => json_encode($entry['expected_entities'] ?? [], JSON_THROW_ON_ERROR),
                'expected_numeric_values' => json_encode($entry['expected_numeric_values'] ?? [], JSON_THROW_ON_ERROR),
                'expected_refusal' => (bool) ($entry['expected_refusal'] ?? false),
                'expected_refusal_reason' => $entry['expected_refusal_reason'] ?? null,
                'expected_language_compliance' => json_encode($entry['expected_language_compliance'] ?? [], JSON_THROW_ON_ERROR),
                'difficulty' => $entry['difficulty'] ?? 'medium',
                'authored_by_user_id' => self::SEEDER_AUTHOR_USER_ID,
                'status' => 'active',
            ];

            // Q13 forward-compat: write the plan_category column too,
            // but only if it exists. The column is deferred per the
            // decision capture; this branch lets the seeder keep
            // working after a future ALTER TABLE.
            if ($this->planCategoryColumnExists()) {
                $row['plan_category'] = $entry['category'] ?? null;
            }

            $existing = DB::table('eval.golden_questions')
                ->where('question_id', $questionId)
                ->exists();

            DB::table('eval.golden_questions')->updateOrInsert(
                ['question_id' => $questionId],
                $row,
            );

            $existing ? $updated++ : $inserted++;
        }

        $this->command?->info(
            "GoldenQuestionsSeeder complete: {$inserted} inserted, {$updated} updated.",
        );
    }

    /**
     * Lazy check for the deferred plan_category column. Cached per-run.
     */
    private function planCategoryColumnExists(): bool
    {
        static $cached = null;

        if ($cached !== null) {
            return $cached;
        }

        $cached = DB::selectOne(<<<'SQL'
            SELECT 1 AS present
            FROM information_schema.columns
            WHERE table_schema = 'eval'
              AND table_name = 'golden_questions'
              AND column_name = 'plan_category'
            LIMIT 1
        SQL) !== null;

        return $cached;
    }
}
