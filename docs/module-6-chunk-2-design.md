# Module 6 Phase B Chunk 2 — Two-Stage Citation Pipeline Design

**Date:** 2026-04-22
**Status:** DRAFT — pending senior-reviewer gate
**Author:** backend-fastapi agent
**Spec refs:** `06-citation-hallucination-guards.md` §6 B1, B5, B8 partial;
              `georag-architecture-addendum-v1.10.html` §04j

---

## 1. Overview

Chunk 2 implements the two-stage citation pipeline behind a feature flag
(`CITATION_SPAN_RESOLVER_ENABLED`, default `false`). When the flag is off,
the system behaves exactly as Chunk 1 left it (legacy dash-form markers,
no rows written to `answer_citation_items` or `answer_citation_spans`).
When the flag is flipped by the apply dispatch, the new path activates.

### Sequence diagram

```
Request arrives
     │
     ▼
[Orchestrator] _classify_query()
     │
     ▼ (flag=true)
[Stage 1] bind_evidence(tool_results) → BoundEvidenceSet
     │  assigns [DATA:N], [NI43:N], [PUB:N], [PGEO:N] (colon form)
     │
     ▼
[LLM] system prompt with colon-form citation discipline
      receives Evidence Set block with marker slots
     │
     ▼  LLM returns answer text with colon-form markers
[Stage 2] resolve_spans(answer_text, bound_set) → (items, spans_per_item, telemetry)
     │  normalizes any stray dash-form markers (defensive rewrite)
     │  computes character offsets for each marker occurrence
     │  looks up FK targets in bound_set
     │
     ▼
[DB] insert_citation_items → get UUIDs (RETURNING)
[DB] batch_insert_citation_spans (back-filled with real UUIDs)
     │
     ▼
[Lifecycle] committed
```

When `flag=false`: Stage 1 is skipped (`_bound_set = None`), legacy
`assign_citation_ids` runs, and the Stage 2 block is skipped entirely.

---

## 2. Marker format table

| Source       | Legacy (flag=false) | New (flag=true) | DB CHECK accepts      |
|--------------|--------------------|-----------------|-----------------------|
| Spatial/graph/assay | `[DATA-N]`  | `[DATA:N]`      | `[DATA:identifier]`   |
| NI 43-101 chunks   | `[NI43-N]`  | `[NI43:N]`      | `[NI43:identifier]`   |
| Publications       | `[PUB-N]`   | `[PUB:N]`       | `[PUB:identifier]`    |
| Public geoscience  | `[PGEO-N]`  | `[PGEO:N]`      | `[PGEO:identifier]`   |
| Evidence items     | _(future)_  | `[ev:<short>]`  | `[ev:identifier]`     |

The `answer_citation_items_marker_shape` DB CHECK requires
`^\[(DATA|NI43|PUB|PGEO|ev):[A-Za-z0-9-]+\]$` — colon separator mandatory.
Only colon-form markers are insertable. Dash-form markers coming from legacy
prompts or stray LLM behaviour are rewritten by `_normalize_markers` before
the primary regex runs.

**Dual-support window:** Dash-form markers that survive normalization (i.e.,
were rewritten to colon-form) count as `legacy_dash_rewrites` in telemetry.
This window closes when Chunk 3's completeness guard is live and actively
rejects responses containing unresolved dash-form markers.

---

## 3. Stage 1 — Evidence binding (`citation_binding.py`)

### Input
- `workspace_id: UUID` — FK context
- `tool_results: list[(tool_name, result_object)]` — same shape as the
  existing `assign_citation_ids` input in `response_assembler.py`
- `evidence_items: list | None` — placeholder for B8.5 non-passage items
  (no rows exist today; pass `None` until the behavioral enable)

### Output
`BoundEvidenceSet` with one `BoundEvidence` per unique marker.

### Counter logic
A shared counter increments across all tool types, matching the existing
`assign_citation_ids` behaviour so existing tests and prompt examples remain
valid. `PublicGeoscienceSearchResult` increments once per record (not per
tool call).

### BoundEvidence fields

| Field          | Type              | Description                                               |
|----------------|-------------------|-----------------------------------------------------------|
| `marker_text`  | `str`             | `[DATA:1]`, `[ev:019d74a7]`, etc.                        |
| `kind`         | `MarkerKind`      | `DATA`/`NI43`/`PUB`/`PGEO`/`ev`                          |
| `index_or_id`  | `str`             | Slot number (tool-slot) or short UUID (ev)                |
| `source_store` | `str`             | `qdrant`/`neo4j`/`postgis`/`hybrid`                       |
| `evidence_id`  | `UUID | None`     | Populated for `[ev:*]` bindings only at Stage 1           |
| `passage_id`   | `UUID | None`     | Populated for `[ev:*]` bindings only at Stage 1           |
| `display_ref`  | `dict | None`     | Opaque metadata for evidence inspector                    |
| `preview_text` | `str`             | First 200 chars of source text (for prompt Evidence Set)  |

**Key limitation:** Tool-slot bindings (`[DATA:N]` etc.) have `evidence_id=None`
and `passage_id=None` at Stage 1. Chunk 3 will wire passage_id resolution
(looking up the Qdrant chunk_id → `document_passages.passage_id`) so these
bindings become fully insertable. In Chunk 2, only `[ev:*]` bindings (which
carry an evidence_id from Stage 1) produce rows in `answer_citation_items`.

---

## 4. Stage 2 — Span resolver (`span_resolver.py`)

### `_normalize_markers(answer_text) → (normalized_text, count_rewritten)`
Rewrites `[DATA-N]` → `[DATA:N]` etc. for any legacy dash-form the model
emitted. Uses `_LEGACY_DASH_RE = re.compile(r"\[(DATA|NI43|PUB|PGEO)-(\d+)\]")`.
`[ev:*]` markers have no dash form; they are never rewritten.

### Primary regex
`_MARKER_RE = re.compile(r"\[(DATA|NI43|PUB|PGEO|ev):([A-Za-z0-9-]+)\]")`
Matches colon-form markers only. Runs after normalization.

### FK resolution
After normalization, each unique `marker_text` is looked up in `bound_set`:
- **Hit with FK target** (evidence_id or passage_id non-None) → `AnswerCitationItemCreate` created.
- **Hit without FK target** (tool-slot binding, both FKs None) → treated as unresolved. No INSERT. Telemetry only. This is the expected state for `[DATA:N]` markers in Chunk 2.
- **Miss** (marker_text not in bound_set at all) → unresolved. Telemetry only.

### Return shape
`(items, spans_per_item, telemetry)` where:
- `items[i]` is one `AnswerCitationItemCreate`
- `spans_per_item[i]` is all `AnswerCitationSpanCreate` for `items[i]`

Spans have `answer_citation_item_id = nil UUID`. The orchestrator calls
`insert_citation_items()` (which returns UUIDs via RETURNING), then
back-fills `answer_citation_item_id` on each span before calling
`batch_insert_citation_spans()`.

### Telemetry fields
| Key | Type | Meaning |
|-----|------|---------|
| `total_markers_found` | int | Raw occurrence count |
| `unique_markers` | int | Distinct `marker_text` values |
| `markers_resolved` | int | Items created |
| `markers_unresolved` | int | Markers skipped |
| `legacy_dash_rewrites` | int | Occurrences rewritten |
| `fully_resolved` | bool | `markers_unresolved==0 and total>0` |
| `partial_resolution_rate` | float | `resolved / unique` (0.0 when unique=0) |
| `normalized_text` | str | Answer text post-normalization |

---

## 5. Feature flag behaviour and rollback

### Enable
```bash
# In .env
CITATION_SPAN_RESOLVER_ENABLED=true

# Restart FastAPI to pick up the new value:
docker compose restart fastapi
# OR (zero-downtime on multi-replica):
docker compose up -d --no-deps fastapi
```

### Rollback
```bash
CITATION_SPAN_RESOLVER_ENABLED=false
docker compose restart fastapi
```
Setting the flag to false immediately restores the legacy dash-form path.
Rows already written to `answer_citation_items` / `answer_citation_spans`
are not deleted — they form an audit trail for the flipped window.

### Smoke check after flip
```bash
# Flag default must be false
docker exec georag-fastapi python -c \
  "import app.config as c; print('flag default:', c.settings.CITATION_SPAN_RESOLVER_ENABLED)"

# Imports must succeed even when flag=false
docker exec georag-fastapi python -c \
  "from app.agent.citation_binding import bind_evidence; \
   from app.services.span_resolver import resolve_spans; \
   from app.services.answer_run_store import insert_citation_items, batch_insert_citation_spans; \
   print('imports ok')"
```

---

## 6. `citation_mode` population

All answer runs currently insert `citation_mode='posthoc_span_resolution'`
(set in the draft INSERT, Chunk 1). This remains correct for Chunk 2:

| State | citation_mode | Notes |
|-------|--------------|-------|
| flag=false (today) | `posthoc_span_resolution` | Legacy path, no DB rows written |
| flag=true, all resolved | `posthoc_span_resolution` | Items + spans written |
| flag=true, partial resolve | `posthoc_span_resolution` | Items for resolved markers only |
| Chunk 4 (future) | `hybrid_delayed_attachment` | Added when fallback is needed |

`hybrid_delayed_attachment` is deferred to Chunk 4, which adds the fallback
branch when `partial_resolution_rate` is below a threshold.

---

## 7. Open items for senior-reviewer

### OFR-1: FK deletion behaviour on `answer_citation_items.evidence_id`

The Chunk 1 migration used `ON DELETE SET NULL` for the `evidence_id` FK. This
matches the `passage_id` FK but diverges from `evidence_items.passage_id`
(which is `ON DELETE RESTRICT`). The Chunk 1 decision doc noted this as
deferred:

> Is `SET NULL` right for audit integrity, or should it be `RESTRICT` like
> `evidence_items.passage_id`?

`SET NULL` allows evidence rows to be deleted from the DB while preserving the
citation audit trail (the `answer_citation_items` row remains, with `evidence_id`
nulled). `RESTRICT` prevents evidence deletion while any citation references it.
For a legal-compliance context (NI 43-101 audit trail), `RESTRICT` is arguably
safer. Recommendation: upgrade to `RESTRICT` in a future additive migration
once the evidence write path is stable. Flag for senior-reviewer decision.

### OFR-2: `[ev:<short-id>]` 8-char truncation collision risk

`_short_ev_id` returns `uuid.hex[:8]` (first 8 hex chars = 32 bits). Within
a single answer run with ≤ 50 evidence items, birthday collision probability is
`1 - exp(-n²/(2 × 2^32))` ≈ 0.00003% for n=50. Effectively zero per-run.

Across workspaces it is non-zero but the bound set is per-answer-run so
collision would only affect one run. The `[ev:*]` bindings are keyed within
`BoundEvidenceSet.by_marker` per run — a cross-workspace collision would
manifest as a wrong-item binding for that run only.

**Proposal:** 8 chars is acceptable. Extend to 12 if we ever see a real
collision in logs. Code already handles the collision case by extending to 12
chars. Accept or propose a different length.

### OFR-3: `partial_resolution_rate` — separate column on `answer_runs`?

Currently `citation_mode` captures the mode (`posthoc_span_resolution` vs
`hybrid_delayed_attachment`) but not the numeric rate. The rate lives in
`telemetry` (logged, not persisted). If the dashboard needs to trend
partial-resolution rate over time, a `partial_resolution_rate NUMERIC(5,4) NULL`
column should be added to `answer_runs`.

**Proposal for senior-reviewer:** Add `partial_resolution_rate` to `answer_runs`
in Chunk 4 (same migration as `hybrid_delayed_attachment` mode column, if added).
Or add a tiny additive migration in Chunk 2. Decision needed before Chunk 4
is drafted.

### OFR-4: Model emits marker not in bound_set (e.g. `[DATA:99]` with only 3 slots)

Current behaviour: treated as unresolved, recorded in telemetry only. The
marker is NOT inserted as a citation_item.

**Proposal:** This should eventually be a guard rejection (Chunk 3). For Chunk
2, the current behaviour (unresolved → telemetry) is correct. Senior-reviewer
to confirm that "unresolved marker = telemetry only (no insert)" is the right
Chunk 2 behaviour, and that Chunk 3 should harden this to a guard-level rejection.

### OFR-5: Dash-form deprecation timeline

Currently the span resolver accepts both dash-form (via normalization) and
colon-form. The dual-support window is open until:
  1. The feature flag is flipped (apply dispatch after this review).
  2. Chunk 3's completeness guard is live and actively rejects responses
     containing unresolved markers.

**Proposal:** Close the dual-support window (remove `_LEGACY_DASH_RE` from
`span_resolver.py`) when Chunk 3 ships. The guard would then treat any
dash-form marker as a "malformed marker" and reject the response. This
incentivises the model to stop emitting dash-form within one or two runs
after the apply dispatch. Senior-reviewer to approve timeline.

---

## 8. Test coverage

### `test_citation_binding.py` — 20 tests
- `_short_ev_id`: length=8, no hyphens
- `bind_evidence`: spatial→DATA, graph→DATA, NI43-doc→NI43, PUB-doc→PUB,
  PGEO→one-per-record, mixed counter, empty, evidence_items→ev markers,
  combined tools+evidence_items
- `BoundEvidenceSet.get`: hit, miss
- `render_evidence_block`: non-empty when bindings exist, empty when no bindings

### `test_span_resolver.py` — 22 tests
- `_normalize_markers`: dash→colon, multiple, mixed, no markers, PGEO, ev unchanged
- `resolve_spans`: 3-all-resolved, partial, marker-not-in-set, same-marker-twice,
  no-markers, no-FK-target, legacy-dash-resolved, mixed-legacy-colon, span offsets,
  ev-marker-resolved, telemetry-keys, normalized-text-type, unknown-store→None,
  spans-per-item-parallel, nil-uuid-in-spans

---

## Chunk 3 Addendum — applied 2026-04-22

This section records the changes made in Chunk 3 that build directly on Chunk 2
internals.

### Senior-reviewer conditions closed

| Condition | Resolution |
|---|---|
| C1 — normalized_text swap | `response.text = _span_telemetry["normalized_text"]` in orchestrator Stage 2 (see §C1 below) |
| C2 — `_SYSTEM_PROMPT_VERSION` bump | 8 → 9 with comment block; `.env` flag flipped `true` |
| C3 — transactional atomicity | `insert_citation_items_with_spans` (atomic) replaces two-call sequence |

### C1 — Normalized text as canonical answer

The `response.text` field in the `GeoRAGResponse` now comes from
`telemetry['normalized_text']` (the dash→colon-normalized string), not the
raw LLM output. This is the only correct anchor for span character offsets.

Orchestrator change (Stage 2 block):
```python
# C1: use normalized text so span offsets and user-visible string are aligned
response.text = _span_telemetry["normalized_text"]
```

Without this swap, a response containing `[DATA-1]` (dash-form) would have
span offsets computed against the colon-form `[DATA:1]` but `response.text`
still containing the dash-form — a 1-character offset drift per occurrence.

### C3 — Atomic item + span INSERT

`insert_citation_items_with_spans` in `answer_run_store.py`:

```python
async def insert_citation_items_with_spans(
    pool: object,
    items: list[AnswerCitationItemCreate],
    spans_by_item: list[list[AnswerCitationSpanCreate]],
) -> list[UUID]:
    ...
    async with pool.acquire() as conn:
        async with conn.transaction():
            for item, spans in zip(items, spans_by_item):
                row = await conn.fetchrow(ITEM_SQL, ...)
                item_uuid = row["answer_citation_item_id"]
                for span in spans:
                    await conn.execute(SPAN_SQL, ...)
                result_ids.append(item_uuid)
    return result_ids
```

A crash between item INSERT and span INSERT no longer leaves orphaned items.

### passage_id lookup bridge

`resolve_spans` (Stage 2) now calls `_lookup_passage_id` for NI43/PUB/PGEO
tool-slot bindings that carry a `chunk_id` in `display_ref`:

```python
async def _lookup_passage_id(
    chunk_id: str,
    pg_pool: object,
    timeout_s: float = 2.0,
) -> UUID | None:
    ...
    row = await asyncio.wait_for(
        conn.fetchrow(
            "SELECT passage_id FROM silver.document_passages "
            "WHERE embedding_id = $1 LIMIT 1",
            chunk_id,
        ),
        timeout=timeout_s,
    )
    return row["passage_id"] if row else None
```

This closes the gap where NI43/PUB/PGEO bindings had `evidence_id=None` and
`passage_id=None`, causing the `has_target` CHECK to reject every INSERT.

The `citation_binding.py` `bind_evidence()` function was updated to carry
`chunk_id` in `display_ref`:
```python
display_ref={"tool": tool_name, "slot": counter, "chunk_id": first_chunk_id}
```

### Four §04i guards

Guards are implemented in two files:

- `src/fastapi/app/agent/hallucination/orchestrator_validators.py` — guards 3
  and 4 (numerical + entity) tightened
- `src/fastapi/app/agent/hallucination/layer_completeness.py` — completeness
  guard + `evaluate_guards` aggregator

`evaluate_guards()` is the single async entry point called by the orchestrator.
It returns a `GuardBundle`:

```python
@dataclass
class GuardBundle:
    all_passed: bool
    numeric: GuardResult
    entity: GuardResult
    completeness: GuardResult
    failed_guards: list[GuardResult]
```

When `all_passed=False`, the orchestrator writes `rejection_reason` and
transitions to `rejected`. `committed` is NOT written (guard-failed runs do
not reach that state).

### Open items carried forward to Chunk 4

The OFR items from Section 7 remain open:

| OFR | Status |
|---|---|
| OFR-1 (`SET NULL` vs `RESTRICT` on evidence_id FK) | Open — filed in backlog, C4 RESTRICT flip |
| OFR-2 (`[ev:*]` 8-char truncation) | Accepted — extend to 12 if collision observed |
| OFR-3 (`partial_resolution_rate` column) | Open — Chunk 4 migration |
| OFR-4 (unresolved marker = telemetry only) | Confirmed correct for Chunks 2+3; Chunk 4 to harden |
| OFR-5 (dual-support window close) | Deferred — remove `_LEGACY_DASH_RE` when Chunk 3 completeness guard actively rejects dash-form |

Note: OFR-5 is partially addressed. Chunk 3's completeness guard does not yet
treat dash-form markers as malformed (it normalises them via `_normalize_markers`
before checking coverage). Full hardening (treat surviving dash-form as guard
failure) is Chunk 4 scope.
