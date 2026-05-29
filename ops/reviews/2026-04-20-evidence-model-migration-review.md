# Senior Review — Module 3 Phase B3 Evidence Model Migration

**Reviewer:** senior-reviewer (Opus), 2026-04-20
**Milestone:** Module 3 Phase B3 — evidence-model hard-gate migration (addendum §04j)
**Authority:** Global Invariant 14

## Verdict: APPROVE WITH CONDITIONS

The draft is architecturally sound, addendum-faithful, Octane-irrelevant (schema-only), and reversible. One blocker must be fixed before `php artisan migrate`, plus three small tightenings. No redraft required.

## Files reviewed

- `ops/migrations/2026-04-20-evidence-model-migration-plan.md`
- `database/migrations/2026_04_20_130000_create_document_revisions.php`
- `database/migrations/2026_04_20_140000_create_evidence_items.php`
- `database/migrations/2026_04_20_150000_create_structured_record_lineage.php`
- `database/migrations/2026_04_20_160000_backfill_document_revisions.php`
- `src/fastapi/app/models/evidence.py`

## Blocker (must fix before apply)

**`evidence_items.passage_id ON DELETE SET NULL`** is self-contradictory with the type-consistency CHECK. When a passage is deleted, SET NULL triggers, but the resulting row has `evidence_type='document_passage'` and `passage_id IS NULL` — violating both `evidence_items_type_ref_consistent` and `evidence_items_exactly_one_ref`. The transaction fails at FK cascade time.

**Fix:** `database/migrations/2026_04_20_140000_create_evidence_items.php:71` — change `ON DELETE SET NULL` → `ON DELETE RESTRICT`. Update plan doc §"FK Graph" and migration header comment accordingly.

**Reasoning:** RESTRICT forces ingestion code to explicitly null-out / migrate evidence rows before pruning a passage. CASCADE silently destroys citations (violates Invariant 1 citation-first). Tombstone CHECK relaxation allows dangling citations (defeats Layer 5 chunk-provenance guard).

## Tightenings (apply with the blocker fix)

1. **Parameter binding in backfill SQL.** `2026_04_20_160000_backfill_document_revisions.php:103-107` uses string interpolation for SHA sentinel + workspace UUID constants. Not an injection risk (class-private constants) but inconsistent with Laravel convention. Convert to `DB::statement($sql, ['sha' => ..., 'ws' => ...])`.

2. **Deterministic timezone cast in backfill.** `COALESCE(r.created_at::timestamptz, NOW())` uses session `TIMEZONE`. Use `COALESCE(r.created_at AT TIME ZONE 'UTC', NOW())` for determinism across environments.

3. **GIN index debt note.** Add TODO in plan doc Module 6 coordination section: once B8.5 enables non-passage writes, `evidence_items.structured_ref`/`graph_edge_ref`/`map_feature_ref` need GIN indexes. Not this migration (zero rows); Module 6 owns.

## Per-item verdict on drafter-flagged concerns

- **SET NULL vs CHECK** — see Blocker above (RESTRICT)
- **TIMESTAMPTZ vs TIMESTAMP(0)** — ACCEPT as deliberate deviation; mandate TIMESTAMPTZ for all future audit/provenance tables
- **Backfill sentinels (`0*64` SHA + `bronze://unknown/...` URI)** — ACCEPT. NOT NULL preserved, sentinel is filterable, down() targets it precisely. Preferable to relaxing the schema for a one-row legacy case
- **`bronze.provenance` coexistence** — ACCEPT. Not redundant (row-level parser audit vs document-level revision chain). Dual-write required on future ingestion assets — add to Module 3 asset spec, not a migration concern

## Per-invariant assessment

- **Invariant 1 (citation-first)** — PASS (after RESTRICT fix)
- **Invariant 4 (iterative resolution)** — PASS (drafter verified `document_id → silver.reports.report_id` against live migration, not spec)
- **Invariant 12 (data_version authority)** — PASS (no `data_version` column needed on evidence; version is query-time via `answer_runs`)
- **Invariant 14 (hard gate / reversibility)** — PASS (all four down() methods correct; reverse order 160→150→140→130 works; backfill down() targets only sentinel rows)

## Additional advisory

- **`evidence_items.source_date` index** — consider partial `WHERE source_date IS NOT NULL` (graph_edge and map_feature rows typically NULL). Micro-optimization.
- **`structured_record_lineage` cardinality** — current draft permits 1:N. If strictly 1:1 per evidence_items, add UNIQUE on `evidence_id`. SME question.
- **Migration 140000 down() order dependency** — relies on 150000 down() first. Standard Laravel behavior; documented in plan. Acceptable.
- **Pydantic `EvidenceItemCreate` mutual-exclusion** — not enforced in class; only DB CHECK. Module 6 ticket: add `@model_validator(mode='after')`.

## Questions for SME (Kyle)

1. **`structured_record_lineage` cardinality:** 1:1 with evidence_items (add UNIQUE on evidence_id) or 1:N (current draft, supports reparse history)?
2. **`document_revisions.is_current` boolean:** add for single-index "latest revision" lookups, or keep `WHERE superseded_by_revision_id IS NULL` pattern?

## Architecture doc gaps (Module 10 backlog additions)

- Addendum §04j doesn't specify FK cascade semantics for `evidence_items.passage_id` — drafter had to infer. Add one sentence mandating RESTRICT.
- No text on `bronze.provenance` / `document_revisions` coexistence. Add paragraph.
- No cross-reference §04j ↔ Module 3 §6 B8.5 enable-order. Sequence diagram would help.

## Summary for Kyle

The evidence-model draft is sound and spec-faithful. The drafter verified live-schema names against actual migrations (not the spec), flagged three real issues for review, and produced reversible migrations with correct down() methods. **One hard blocker**: `evidence_items.passage_id ON DELETE SET NULL` is self-contradictory — must change to RESTRICT (one-line edit). Three smaller tightenings recommended: parameter binding, AT TIME ZONE UTC, GIN-index debt note. Two SME questions (lineage cardinality, is_current flag) answerable in minutes. One more focused data-engineer dispatch (~15 min of edits), then apply. Re-ping senior-reviewer for a short confirm pass or Kyle self-approves on re-read since changes are mechanical.

## Recommendation

**Data-engineer does one more small, targeted draft round** — not a full redraft. ~15 minutes. Apply directly after Kyle answers the two SME questions.
