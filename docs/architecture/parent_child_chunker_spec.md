# Parent-Child Chunker Spec — Plan §1b

**Status (2026-05-28 post-cutover):** Shipped + canonical + cutover complete. Implementation landed in commit `903a827` (chunker + CHECK migration + docker-compose env passthrough). Per **ADR-0010** (2026-05-28), `silver.document_passages` IS the canonical chunked-content corpus across:

  - Qdrant `georag_chunks` collection — populated by the `index_document_passages` Dagster asset (ADR-0010 Session A, 2026-05-28; 7,065 points)
  - Reranker label-dataset chain — `reranker_chunk_population` reads `silver.document_passages` exclusively (ADR-0010 Session B, commit `d2de95d`)
  - FastAPI `search_documents` retrieval tool — `RETRIEVAL_USE_DOCUMENT_PASSAGES` default is `True` (ADR-0010 Session C, commit `df71a35`)

**Cutover completed 2026-05-28:**

  - Full 119-question golden-set benchmark on `georag_chunks` matched the historical baseline exactly per-slice (delta +0.000 on every question_set; latency drift on 111/119 confirmed independent code path)
  - `georag_reports` Qdrant collection dropped (15,413 points reclaimed)
  - `index_reports` Dagster asset retired from `definitions.py`
  - `RETRIEVAL_USE_DOCUMENT_PASSAGES` default flipped to True; `.env` override present
  - Bench artifacts: `bench_results/adr0010-full-candidate-20260528T151702Z.json`

The previous spec status described §1b as "companion to `parent_expansion` (Plan §3d, shipped commit `7049e20`). §3d is **inert** until this spec is implemented because no `silver.document_passages.parent_chunk_id` values are populated by the existing flat-chunker." That state was resolved at the §1b ship — the §1b chunker IS the canonical ingest path, and the ADR-0010 cutover wired retrieval + reranker training to read from it end-to-end.

**Original spec content preserved below** for reference + provenance. The hard rules + data shape remain accurate; the rollout/feature-flag sections are historical now that the cutover landed.

---

**Original status line:** Spec — companion to `parent_expansion` (Plan §3d, shipped commit `7049e20`). §3d is **inert** until this spec is implemented because no `silver.document_passages.parent_chunk_id` values are populated by the existing flat-chunker. This document defines the ingest-side wire that lights it up.

## Why this exists

The §3d expander appends a passage's parent — a wider section-level chunk — as sibling evidence when a child wins retrieval. This gives the LLM both the precise paragraph AND the surrounding context.

The current chunker (`pdf_ingester._chunk_pages`) emits a flat list of ~800-token narrative chunks with no hierarchy. Every `parent_chunk_id` in production is `NULL`, so `expand_parents` always no-ops with `reason="no parent_chunk_ids on packet"`.

This spec describes the chunker rewrite that emits parent + child pairs with the FK populated.

## Hard rules

1. **Greenfield-only by default.** Existing 7,064 passages keep their `parent_chunk_id=NULL` forever. Backfill is a separate, explicit operation (§5 below).
2. **Feature-flag-gated rollout.** `PARENT_CHUNKING_ENABLED` defaults False. Flip per-environment after dev validation.
3. **Per-ingester opt-in.** PDF first; XLSX/TIFF/CSV/IOgas decide later. Each ingester has its own override path so a regression in one format doesn't block the others.
4. **Idempotent.** Re-ingesting the same source SHA must not create duplicate parents — `ON CONFLICT (document_id, revision_number, text_hash) DO NOTHING` covers it iff parent text is deterministic for a given group of children.
5. **No retrieval-ranking change for existing data.** The flag is checked at chunk-time only; existing passages remain unchanged.

## §1 — Data shape

A "parent" is a section-aligned superchunk containing N contiguous children. The parent's text is the concatenation of its children (with newline separators); the parent's `page_first` / `page_last` is the span of its children.

```
silver.document_passages
├── (parent passage) chunk_kind='section', parent_chunk_id=NULL,
│                    text=concat of children, page span = first→last
│   ├── (child 1)    chunk_kind='paragraph', parent_chunk_id=parent_uuid
│   ├── (child 2)    chunk_kind='paragraph', parent_chunk_id=parent_uuid
│   └── (child N)    chunk_kind='paragraph', parent_chunk_id=parent_uuid
└── (parent passage) ...
```

The existing `chunk_kind` column accepts arbitrary strings — `'narrative'` is the legacy value, `'section'` (parent) and `'paragraph'` (child) are the new values. No schema migration required for `chunk_kind`.

## §2 — Grouping strategy

Two viable strategies; this spec recommends **fixed-N grouping** for v1 because it sidesteps section-detection brittleness and works on any text.

| Strategy | Description | Pros | Cons |
|---|---|---|---|
| **Fixed-N grouping** (v1) | Group every N=3 child chunks into a parent. | Deterministic, format-agnostic, no regex tuning, predictable cost. | Parent boundaries may cut sections. |
| **Section-detection** (v2) | Detect headings via regex (ALL-CAPS lines, numbered "1.0", "1.1") and group children within a section under one parent. | Semantically aligned with document structure. | Heading patterns vary wildly across NI 43-101 reports; misfires would degrade retrieval. |

**v1 chosen.** Section-detection is a follow-up after we measure v1 retrieval quality.

**Parent size**: With N=3 children × 800 tokens/child target = ~2,400 token parents. Stays comfortably under the 8000-char cap on `DocumentEvidence.text` enforced by `parent_expansion.expand_parents_sync` (truncates at `[:8000]`).

**Edge cases**:
- Single child remaining at end of doc → emit as flat narrative (no parent), keeping current behaviour. Avoids 1-child parents that just duplicate the child.
- Children spanning multiple pages → parent inherits `page_first` from first child, `page_last` from last child.
- Very large child (≥ _MAX_CHUNK) → emit as standalone, no parent.

## §3 — UUID generation

The current insert uses SQL-side `gen_random_uuid()`. To set `parent_chunk_id` on children, the parent's UUID must be known at child-insert time. Two options:

| Option | Description | Recommendation |
|---|---|---|
| **A — Two-pass insert** | INSERT parents (RETURNING UUIDs), then INSERT children referencing them. | Heavier rewrite of `_insert_passages`. |
| **B — Pre-generate UUIDs in Python** | `uuid.uuid4()` for parent IDs at chunk-time. Pass through to `INSERT ... VALUES ($1, ...)` instead of `gen_random_uuid()`. | **Chosen.** Smaller diff, no extra round-trip. |

Per ADR-0003 conventions, all UUID generation should be deterministic-ish where possible. Pre-generated UUIDs are non-deterministic but the `ON CONFLICT (document_id, revision_number, text_hash) DO NOTHING` clause already handles re-ingest dedup.

## §4 — Embedding cost analysis

This is the operational concern that gates §1b rollout.

**Current state**: 1 child per chunk → 1 embedding per chunk.
**Post §1b**: 1 child per chunk + 1 parent per N children → N+1 embeddings for every N original chunks.

With N=3: embedding work goes from `N` to `N+1` = **+33%** for any doc that triggers parent emission.

| Cost dimension | Impact at N=3 | Mitigation |
|---|---|---|
| **Qdrant storage** | +33% vector rows + payloads | Acceptable — Qdrant scales linearly + we're at <10K passages today |
| **Embedding compute** | +33% bge-small CPU/GPU time | bge-small on the A4500 hit 144 chunks/sec post-GPU wire (per MEMORY → project_gpu_acceleration); +33% adds ~2-3 sec per typical 200-chunk NI 43-101 |
| **Retrieval candidate set** | +33% pool to rerank | Parent + child compete for the same top-K slots; expander dedupes the parent if it's already in the pool |
| **Re-rank latency** | +33% cross-encoder pairs | bge-reranker-base CPU at 10 candidates = ~500ms; +33% adds ~150ms; acceptable per latency budget |

**Net**: storage + compute costs are linear and small; retrieval quality is the open question. Plan §5b golden-query benchmark on a fixture set will measure whether parents improve answer quality enough to justify the +33% cost. Recommendation: ship dev-only, collect retrieval-quality samples for 1 week, decide on staging promotion.

## §5 — Backfill options

Existing 7,064 passages have `parent_chunk_id=NULL`. Three options for retroactively populating it:

| Option | Description | Cost | Risk | Recommendation |
|---|---|---|---|---|
| **A — No backfill** | Old passages stay flat; only new ingests get parents. | Zero | Inconsistent retrieval behaviour across docs (old NI 43-101s look "smaller" than new ones in retrieval). | **Default**. Acceptable while corpus is small. |
| **B — Per-document re-chunk** | Run a Dagster job: for each report_id, delete existing passages, re-chunk from `bronze.documents.raw_text`, re-insert. | High (re-embed everything, 7K rows × ~3s/chunk-batch = ~5h) | Old chunk UUIDs change → invalidates any external references (Qdrant points need re-sync, citation `passage_id` FKs need careful handling) | Defer; only do if option A's inconsistency proves harmful. |
| **C — Synthetic parent backfill** | Walk existing passages by document_id + ordinal; group every N contiguous → create a parent row (no embedding) → update child `parent_chunk_id`. | Medium (no re-embed, just SQL + UUID writes) | Synthetic parents won't be in Qdrant so retrieval can't surface them directly — they only fire via §3d expansion of a child hit. Acceptable. | **Reserved option** if §3d gives measurable lift on greenfield-only docs and Kyle wants to extend to old corpus. |

**Recommendation**: ship option A. Revisit C only if §3d lift is measurable on greenfield docs.

## §6 — Per-ingester rollout

| Ingester | Recommendation | Reason |
|---|---|---|
| `pdf_ingester.py` | First. Largest corpus + most uniform structure. | Highest §3d value (NI 43-101s have heavy section structure). |
| `xlsx_ingester.py` | Second wave. | XLSX produces table-row passages; parent = sheet? row-group? needs separate thought. |
| `tiff_ocr_ingester.py` | Aligned with PDF (both produce narrative text). | Share the chunker once PDF lands. |
| `csv_ingester.py` (via Dagster) | Out of scope. | CSV rows are atomic; no useful parent shape. |
| IOgas parser | Out of scope. | Same as CSV. |

## §7 — Implementation sketch

### 7.1 `pdf_ingester._chunk_pages` rewrite

```python
def _chunk_pages(pages: list[str]) -> list[dict]:
    if not settings.PARENT_CHUNKING_ENABLED:
        return _chunk_pages_flat(pages)  # current behaviour, renamed

    children = _chunk_pages_flat(pages)  # same paragraph split as today
    return _group_into_parents(children, parents_per_group=3)


def _group_into_parents(children: list[dict], *, parents_per_group: int = 3) -> list[dict]:
    """Emit interleaved parent/child rows.

    Parent passage_id pre-generated; child rows carry it as parent_chunk_id.
    Single-child groups at the tail emit as flat (no parent).
    """
    out: list[dict] = []
    ordinal = 0
    for group_start in range(0, len(children), parents_per_group):
        group = children[group_start : group_start + parents_per_group]
        if len(group) == 1:
            # Single tail child — emit flat, no parent.
            c = dict(group[0])
            c["ordinal"] = ordinal
            c["chunk_kind"] = "narrative"
            c["parent_chunk_id"] = None
            out.append(c)
            ordinal += 1
            continue

        parent_id = str(uuid.uuid4())
        parent_text = "\n\n".join(c["text"] for c in group)
        out.append({
            "passage_id_override": parent_id,
            "ordinal": ordinal,
            "text": parent_text,
            "text_hash": hashlib.sha256(parent_text.encode()).hexdigest(),
            "page_first": group[0]["page_first"],
            "page_last": group[-1]["page_last"],
            "chunk_kind": "section",
            "parent_chunk_id": None,
        })
        ordinal += 1
        for c in group:
            c = dict(c)
            c["ordinal"] = ordinal
            c["chunk_kind"] = "paragraph"
            c["parent_chunk_id"] = parent_id
            out.append(c)
            ordinal += 1
    return out
```

### 7.2 `_insert_passages` change

Add `chunk_kind` + `parent_chunk_id` to the INSERT column list, switch parent-row inserts to use `$N::uuid` instead of `gen_random_uuid()` when `passage_id_override` is set:

```sql
INSERT INTO silver.document_passages
    (passage_id, document_id, workspace_id, revision_number,
     text, text_hash, ordinal, page_first, page_last,
     chunk_kind, parent_chunk_id, created_at, updated_at)
VALUES (COALESCE($10::uuid, gen_random_uuid()), $1::uuid, $2::uuid, $3,
        $4, $5, $6, $7, $8, $9, $11::uuid, NOW(), NOW())
ON CONFLICT (document_id, revision_number, text_hash) DO NOTHING
```

### 7.3 Settings

```python
# app/config.py — new flag
PARENT_CHUNKING_ENABLED: bool = False
PARENT_CHUNKING_GROUP_SIZE: int = 3  # children per parent
```

## §8 — Test plan

| Test | Asserts |
|---|---|
| Flag off → byte-identical output to current `_chunk_pages` | No regression on existing behaviour |
| Flag on + 6 children → emits 2 parents + 6 children = 8 rows | Grouping works at exact multiple |
| Flag on + 7 children → 2 parents + 6 children + 1 flat tail = 9 rows | Tail-single handled gracefully |
| Flag on + 1 child → 1 flat row, no parent | Edge: tiny doc |
| Parent text = concat of children with `\n\n` separator | Reconstruction correctness |
| Parent `page_first` = first child's `page_first`, `page_last` = last child's `page_last` | Page-span correctness |
| Parent `chunk_kind='section'`, children `chunk_kind='paragraph'` | Discriminator values match `parent_expansion._FETCH_PARENTS_SQL` SELECT |
| Children carry parent's UUID in `parent_chunk_id` | FK populated |
| Re-ingest same SHA → ON CONFLICT skips parents and children | Idempotency |
| Insertion order: parent BEFORE children | FK constraint satisfied at INSERT time |

Test file: `src/fastapi/tests/test_parent_child_chunker.py`. ~12 unit tests + 1 integration with a fixture PDF.

## §9 — Rollout staging

| Stage | Trigger | Action |
|---|---|---|
| **Dev** | After this spec is approved + implementation lands | Flip `PARENT_CHUNKING_ENABLED=True` in dev `.env` only; re-ingest 1-2 NI 43-101 reports to populate; verify trace inspector shows `expand_parents` adding sibling evidence |
| **Staging** | After 1 week of dev signal + `gold.repair_shadow_daily` or golden-query benchmark shows §3d lifting answer quality | Flip in staging `.env`; new ingests populate |
| **Prod** | After staging telemetry confirms no retrieval-quality regression | Flip in prod `.env`; greenfield-only; existing chunks unchanged |

EXIT-gate criteria for each stage: zero increase in guard failure rate, ≤ 10% increase in p95 ingest latency, ≥ 5% increase in retrieval quality on golden-query benchmark (measured via `eval/golden_queries` harness shipped in §28).

## §10 — Open questions for SME review

1. **Group size**: N=3 (~2,400 token parents) vs N=4 (~3,200 token parents) vs N=5. Bigger parents = more context but more redundancy with children. Default N=3 unless Kyle has a preference.
2. **Backfill priority**: Is option A acceptable forever, or does Kyle want to schedule option C after greenfield validation?
3. **XLSX strategy**: Defer to a separate spec (Plan §1b-XLSX). Flagging here so it doesn't get lost.

## References

- `parent_expansion.py` (Plan §3d, commit `7049e20`) — the consumer this enables
- `OVERNIGHT_LOG.md` §34 — describes §3d as "inert until §1b ingest populates parent_chunk_id"
- `database/migrations/2026_05_28_030000_add_parent_chunk_id_to_document_passages.php` — the DDL this spec writes to
- MEMORY: `project_gpu_acceleration_2026_05_22` — bge-small GPU baseline (144 chunks/sec)
- MEMORY: `project_parse_perf_2026_05_22` — chunker perf history
