# Golden question seed loader — design

**Status:** Spec for the loader that reads `tests/golden_questions/seed_template.yaml` into `eval.golden_questions`. **Not implemented in this overnight run.**

## Why this loader is needed

`eval.golden_questions` already exists (migration `2026_05_13_140000_create_eval_schema.php`). What's missing is a way to keep its rows in sync with the SME-edited YAML source.

Two reasonable patterns:

1. **Idempotent Laravel seeder** (`database/seeders/GoldenQuestionsSeeder.php`) that reads the YAML and upserts by `id` (mapping to `question_id` UUID via deterministic UUID v5 from the kebab `id`).
2. **Dagster asset** under `src/dagster/georag_dagster/assets/` that materialises the YAML into the table, with the same idempotency.

Either pattern works. The seeder is simpler and what most Laravel projects expect. The Dagster asset is what makes sense if the YAML lives alongside other eval data assets.

## Recommended pattern: Laravel seeder

Why: the table is read by both Laravel-side eval dashboards and FastAPI-side eval runs. Laravel seeders compose with `php artisan db:seed --class=GoldenQuestionsSeeder` and integrate with the existing migration cadence Kyle uses.

### Skeleton

```php
<?php

namespace Database\Seeders;

use Illuminate\Database\Seeder;
use Illuminate\Support\Facades\DB;
use Symfony\Component\Yaml\Yaml;

class GoldenQuestionsSeeder extends Seeder
{
    public function run(): void
    {
        $path = base_path('tests/golden_questions/seed_template.yaml');
        $data = Yaml::parseFile($path);

        foreach ($data['categories'] as $entry) {
            $questionId = \Ramsey\Uuid\Uuid::uuid5(
                \Ramsey\Uuid\Uuid::NAMESPACE_OID,
                $entry['id']
            )->toString();

            DB::table('eval.golden_questions')->updateOrInsert(
                ['question_id' => $questionId],
                [
                    'question_set'  => $entry['question_set'] ?? 'core_chat',
                    'question_text' => $entry['question'],
                    'context_setup' => json_encode([
                        'plan_category'  => $entry['category'],
                        'source_id'      => $entry['id'],
                        'notes'          => $entry['notes'] ?? null,
                    ]),
                    'expected_intent_class' => $entry['expected_intent_class'] ?? null,
                    'expected_citations'    => json_encode($entry['expected_citations'] ?? []),
                    'expected_entities'     => json_encode($entry['expected_entities'] ?? []),
                    'expected_numeric_values' => json_encode($entry['expected_numeric_values'] ?? []),
                    'expected_refusal'        => $entry['expected_refusal'] ?? false,
                    'expected_refusal_reason' => $entry['expected_refusal_reason'] ?? null,
                    'difficulty'              => $entry['difficulty'] ?? 'medium',
                    'authored_by_user_id'     => 1,
                    'status'                  => 'active',
                ]
            );
        }
    }
}
```

### Why UUID v5 from `id`?

Deterministic. Re-running the seeder with the same YAML never creates duplicate rows. SME-renaming a question's `id` produces a NEW row and leaves the old one (so the answer history attached to the old question_id remains queryable).

### Why store plan-§5a category in `context_setup` JSONB?

`golden_questions.question_set` has a CHECK constraint allowing 8 values (`core_chat`, `public_private_boundary`, `numeric_grounding`, `refusal_correctness`, `target_recommendation`, `report_section`, `schema_mapping`, `ocr_triage`). Plan §5a defines 20 categories. The mapping in `seed_template.yaml` picks the closest `question_set` for the schema constraint, while keeping the plan-§5a category in `context_setup.plan_category` for evaluation reports that need the full 20-way breakdown.

If we'd rather have the plan-§5a category as a first-class column, add a migration:

```sql
ALTER TABLE eval.golden_questions ADD COLUMN plan_category VARCHAR(80);
CREATE INDEX idx_gq_plan_category ON eval.golden_questions (plan_category);
```

(Not added by this overnight run — it's a one-line change but it touches the existing eval schema, which is conservative-zone territory.)

## Loader for FastAPI-side eval runs

`src/fastapi/app/services/eval/real_rag_evaluator.py` already exists (see modified files in `main` WIP). After the seeder lands, the evaluator reads from `eval.golden_questions` directly — no separate Python loader needed.

## Acceptance criteria (plan §5a, applied to seed scaffolding)

- [x] 20 categories scaffolded in YAML — DONE
- [x] 33 seed questions covering 16/20 categories — DONE (4 categories still at 0 — categories 6 was sparse but covered; gaps noted at YAML bottom)
- [ ] Seeder reads YAML and idempotently writes to eval.golden_questions — NOT IMPLEMENTED
- [ ] All 20 categories have ≥ 2 questions — YAML currently has 1 question for several categories; SMEs to expand
- [ ] Evaluation harness runs the seed against the live pipeline — NOT IN SCOPE for this overnight run (depends on the loader landing first)

## Decisions captured — 2026-05-27 morning

Kyle reviewed and accepted all four recommendations:

| Q | Decision | Implication |
|---|---|---|
| Q13 | **Add `plan_category VARCHAR(80)` as first-class column** on `eval.golden_questions` when per-§5a-category dashboards land | Until then, `plan_category` stays in `context_setup` JSONB (seeder writes both `context_setup.plan_category` AND `plan_category` column if column exists — backward-compatible). Migration deferred. |
| Q14 | Seeder `authored_by_user_id` needs **Kyle's real user ID** | Placeholder in seeder + clear `// TODO: set BEFORE running` comment. Kyle resolves at apply time. |
| Q15 | **YAML is committed source-of-truth; SMEs PR additions; seeder re-runs on deploy** | The deploy pipeline (or `composer.json` post-deploy hook) runs `php artisan db:seed --class=GoldenQuestionsSeeder` after migrations. Idempotent — re-runs are safe. |
| Q16 | **Both** Hatchet nightly cron (trend tracking) AND on-demand `/api/v1/eval/run` (ad-hoc validation) | Cron already mostly exists in WIP (`eval_real_rag_nightly.py`). The on-demand route lives at `routes/api.php` — small additive endpoint that triggers the same runner with `triggered_by='manual'`. |
