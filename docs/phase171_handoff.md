## Doc-phase 171 handoff — §04i failure-layer breakdown panel on Eval Dashboard

**Status:** Live + 108/108 substrate verifier + live DB data shows the panel reads operator-meaningful signal.

## What landed

With all 6 §04i validators graduated (doc-phase 168) and the nightly
real_rag_v1 cron firing 24 h cadence (doc-phase 170), the Eval
Dashboard now surfaces the per-layer failure breakdown. When a
nightly cron flags a regression, the operator's first triage
question — "which validator caught it?" — gets an immediate answer
without a SQL drilldown into `eval.run_results`.

### Controller — `EvalDashboardController::failureLayerBreakdown()`

Aggregates `eval.run_results.failure_layer` across failed rows joined
to `eval.run_summaries.started_at >= now() - interval '30 days'`,
then **merges with the canonical 8-bucket layer set** so every
render carries all layers in chain order:

| Order | Bucket | Source |
|---|---|---|
| 0 | `6_refusal` | Layer 6 (doc-phase 159 / 163) |
| 1 | `2_citation_presence` | Layer 2 (doc-phase 163) |
| 2 | `5_chunk_provenance` | Layer 5 (doc-phase 165) |
| 3 | `4_entity_resolution` | Layer 4 (doc-phase 166) |
| 4 | `3_numeric_claims` | Layer 3 (doc-phase 167) |
| 5 | `1_retrieval_quality` | Layer 1 (doc-phase 168) |
| 6 | `refusal` (legacy) | pre-doc-phase-163 real_llm_v1 |
| 7 | `evaluator_not_ready` | infra — vLLM unreachable etc. |

Zero-count buckets still appear (muted in the UI) so the panel is
shape-stable: operators see all §04i layers + infra row every render,
not just the failing ones. Forward-compat: any non-canonical bucket
(future validators) lands at the end so it never gets silently dropped.

### UI — `EvalDashboard.tsx`

New `§04i failure-layer breakdown` section above the recent-runs
table. Per row:

  - **Label** — "Layer N — name" or "Infra — evaluator_not_ready"
  - **Bar** — width normalized to the peak count in the window;
    sky-blue for layer buckets, amber for infra, muted for zero
  - **Count** — actual fail count, or "—" when zero
  - **Last failed** — formatted timestamp of the most recent fail
    in the bucket

Header subtitle shows aggregate: "no validator failures" when the
window is fully green, or "N failures across M layers" otherwise.

### Live data

Live DB read returns 8 buckets cleanly:

```
0  6_refusal             fail_count=0   last=null
1  2_citation_presence   fail_count=0   last=null
2  5_chunk_provenance    fail_count=8   last=2026-05-14 03:26
3  4_entity_resolution   fail_count=0   last=null
4  3_numeric_claims      fail_count=0   last=null
5  1_retrieval_quality   fail_count=0   last=null
6  refusal (legacy)      fail_count=15  last=2026-05-14 02:59
7  evaluator_not_ready   fail_count=0   last=null
```

The 8 `5_chunk_provenance` rows are residue from the doc-phase 165
Layer 5 development churn before the DATA-citation skip landed. The
15 `refusal` rows are doc-phase 159 real_llm_v1 cuts before the
doc-phase 163 `6_refusal` rename. Both are historical fixed-state —
the panel surfacing them gives the operator confidence that the
underlying data is real, not synthetic placeholder.

## Tests — 2 cases added

`tests/Feature/Admin/Track3DashboardsTest.php` — extended the
existing Inertia-render smoke for the Eval Dashboard:

| Case | Verifies |
|---|---|
| `test_eval_dashboard_admin_renders_with_expected_props` (extended) | New `failure_layer_breakdown` prop is in the Inertia payload |
| `test_eval_dashboard_failure_layer_breakdown_returns_canonical_buckets` (new) | Returns exactly 8 buckets in canonical chain order, every bucket has `fail_count` + `last_failed_at` |

Both gated on `RequiresPostgres` — pre-existing migration-replay
trap with `CREATE POLICY` blocks them under the project's
RefreshDatabase+pgsql_test workflow. The substrate verifier check
exercises the schema-shape contract instead (executed_at column
existence), which is the actual contract the controller depends on.

## Smoke verification

```bash
# Live controller invocation against live DB (via reflection)
docker exec georag-laravel-octane php artisan tinker --execute='...'
# → rows=8, all canonical buckets present, live counts populated

# Substrate verifier
bash scripts/autonomous_run_substrate_verify.sh
# → 108/108 checks passed (was 107 — +1 column-existence check)
```

## Cumulative session state — 40 ticks closed

- **Doc-phase ticks this run:** **40** (132 → 171)
- **Substrate verifier:** **108/108 PASS**
- **Live pytest cases:** 284
- **Laravel test cases (Track3 dashboards):** 14 (12 + 2)
- **Sections closed:** §25.4 + §6 + §04i validators
- **§04i validators:** 6 of 6 — graduated + **dashboard-surfaced**
- **Evaluator kinds wireable:** 3
- **Hatchet AI pool workflows:** 12
- **§10.6 nightly cron:** live
- **§21.3 types covered:** 8 of 8
- **PublicGeo features on map:** 95

## What's next

The eval pipeline is now end-to-end visible:
- run → §04i 6-layer chain → per-question result → per-layer breakdown panel
- Nightly cron exercises the full path 24/7
- Operator sees layer-level failure trend on `/admin/eval-dashboard`

Productive next directions (carried over):

- **Fix the bge-reranker-base ONNX config** so Layer 5 sharpens
- **Ingest a sample project's documents** so retrieval surfaces real
  chunks (turns refusal-only signal into full-chain signal)
- **Wire the cron's `failure_summary` into a Slack / PagerDuty webhook**
  via `external_notification` workflow
- **SME-author core_chat / public_private_boundary / target_recommendation**
  question sets — exercises Layers 1-5 in non-vacuous mode
- **Re-link the test-DB RefreshDatabase trap**: `CREATE POLICY` lacks
  `IF NOT EXISTS` — add idempotent variants to the 4 policy migrations
  so `phpunit.pgsql.xml` runs cleanly. Unblocks the new
  `test_eval_dashboard_failure_layer_breakdown_*` assertions.

## Carry-overs

- The Laravel Inertia test that asserts on the canonical 8-bucket
  ordering is gated behind `RequiresPostgres` + RefreshDatabase.
  Today the RefreshDatabase path is blocked by a pre-existing
  `CREATE POLICY` idempotency gap in the targeting migration. The
  substrate-verifier check + the live-data tinker invocation cover
  the contract today; when the policy idempotency lands, the
  feature-test assertions provide the same coverage formally.
- The "refusal" (legacy) bucket will drift to zero over time as
  doc-phase 159's real_llm_v1 evaluator cuts roll out of the
  30-day window. When count = 0 + last_failed_at = null for a
  full window, the bucket can be retired from the canonical list.
- The bar normalization is peak-count-relative. In a clean steady
  state (no failures), bars render at the muted stone tone — visually
  signals "all layers safe" without changing the panel's vertical
  height. Layout stable.
