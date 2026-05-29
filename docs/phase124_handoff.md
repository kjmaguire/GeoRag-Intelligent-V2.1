## Doc-phase 124 handoff — Track 2 prep: §10.2 mechanical golden questions

**Status:** 45 mechanical questions scaffolded. **10/10 pure-shape pytest pass.**
4 live DB tests queued for after the rebuild finishes.

## What landed

### Package layout

`src/fastapi/app/services/eval/mechanical_questions/`:
- `__init__.py` — re-exports + `ALL_MECHANICAL_QUESTIONS` aggregate
- `numeric_grounding.py` — **15** questions exercising §04i layer 3
- `schema_mapping.py` — **10** questions exercising §04 schema decisions
- `ocr_triage.py` — **10** questions exercising §04p quality_graph routing
- `report_section.py` — **10** questions exercising §15.1/§15.2 templates
- `seed_runner.py` — idempotent seeder with stable per-question UUIDs
- `__main__.py` — CLI (dry-run by default; `--commit` to apply)

**45 mechanical questions total** — the autonomous-safe half of the
§10.2 100-question target. The 50 SME-authored questions
(core_chat, public_private_boundary, refusal_correctness,
target_recommendation) follow the same template once Kyle authors them.

### Stable UUIDs

Each question's `question_id` is derived from `SHA-256(question_set |
question_text)` packed into a UUID v4 envelope. Re-seeding produces
identical IDs — `eval.run_results.question_id` references stay
stable across seed iterations.

### Idempotent seeder behavior

| Scenario | Outcome |
|---|---|
| First seed against empty table | `inserted=45, updated=0, unchanged=0` |
| Re-seed unchanged | `inserted=0, updated=0, unchanged=45` |
| Re-seed with one mutated question (e.g. difficulty change) | `inserted=0, updated=1, unchanged=44` |
| Empty list | `total_processed=0` no-op |

### Per-set question contracts

**numeric_grounding (15)** — every question has `expected_numeric_values[]`
with `{path, unit, tolerance_pct, source_table}`. Verifier asserts the
chat answer's value matches the silver row within tolerance.

**schema_mapping (10)** — every question has one `expected_entities[]`
entry with `{raw_column, canonical_table, canonical_column,
unit_conversion?}`. Verifier asserts the mapping decision matches.

**ocr_triage (10)** — every question has `expected_entities[]` with
`{expected_route, expected_reason}`. Verifier asserts `route_page()`
returns the same tuple. Covers all 4 routes (accept/re_ocr/silver_review/reject)
+ reasons (map_heavy_v1_deferral, retry_max_exceeded,
ocr_confidence_below_threshold, etc.).

**report_section (10)** — every question has `expected_entities[]` with
`required_section_ids`. Verifier asserts the generated `sections_plan`
contains every listed id. **Anchored to the real §15.2 template
manifest** — `test_report_section_required_ids_match_template_slugs`
catches any drift between questions and templates.

### Pure-shape pytest — 10 cases, all pass in 0.12s

| Test | Verifies |
|---|---|
| `test_question_counts` | 15 + 10 + 10 + 10 = 45 total |
| `test_question_sets_match_module` | Each question's set matches its module |
| `test_required_fields_present` | question_set + question_text + difficulty on every Q |
| `test_question_texts_unique_per_set` | No (set, text) collision |
| `test_stable_question_id_is_deterministic` | Hash-derived UUID stable |
| `test_refusal_questions_have_reason` | Refusal Qs include reason |
| `test_numeric_grounding_expected_values_well_formed` | Per-question shape |
| `test_schema_mapping_expected_entities_well_formed` | Per-question shape |
| `test_ocr_triage_expected_routes_valid` | Routes ∈ valid 4-tuple |
| `test_report_section_required_ids_match_template_slugs` | Anchored to real template |

### Live DB pytest (4 cases) — queued for post-rebuild

| Test | Will verify |
|---|---|
| `test_first_seed_inserts_all_45` | inserted=45 from a clean slate |
| `test_reseed_is_idempotent` | unchanged=45 on identical re-seed |
| `test_changed_question_triggers_update` | updated=1, unchanged=44 on a single diff |
| `test_seed_empty_list_is_noop` | empty list → zero report |

These need the rebuild + a healthy fastapi container to run.

### CLI

```bash
# Dry-run (default):
docker exec georag-fastapi python -m \
    app.services.eval.mechanical_questions

# Apply with author user-id 1:
docker exec georag-fastapi python -m \
    app.services.eval.mechanical_questions --user-id 1 --commit
```

### Verifier extension

Added `_check_pytest_module "mechanical_questions"` to
`scripts/autonomous_run_substrate_verify.sh`. Once the rebuild
lands + DB tests pass, the verifier should be **70/70** (was 69/69
pre-rebuild + 1 new gate).

## How this fits the §10 done-test

Per §10 acceptance: "a candidate prompt change triggers eval, the
eval blocks promotion on a regression."

With these 45 mechanical questions seeded:
- The §10.4 `evaluate_workspace` Hatchet workflow has 45 active
  questions to run against any candidate.
- §10.6 regression thresholds (already live from doc-phase 99) gate
  promotion based on the run result.
- §2.9 public/private boundary + §18.1 target language enforcement
  questions still pending Kyle (in the 50 SME questions) — those
  are the highest-stakes regression gates (zero-tolerance per the
  §10.6 thresholds).

## Carry-overs

- **Live DB pytest tests** queued; will run automatically once
  fastapi container is healthy post-rebuild.
- **Seeder run** — Kyle (or me on his go-ahead) executes
  `python -m app.services.eval.mechanical_questions --user-id N --commit`
  once the rebuild is settled. Lands 45 active rows in
  `eval.golden_questions`.
- **50 SME-authored questions** — different module (e.g.
  `sme_questions/`) follows the same pattern but with
  `status='draft'` until Kyle reviews each.
- **Question content depends on test fixture** — the
  numeric_grounding questions reference the 20-collar test corpus
  (project_slug="lazy-edward-bay"). Eval runs require that fixture
  present; doc tests are independent.
