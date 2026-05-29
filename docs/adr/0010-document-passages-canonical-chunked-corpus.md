# ADR 0010: silver.document_passages is the canonical chunked-content corpus

- **Date**: 2026-05-29
- **Status**: Accepted
- **Deciders**: Kyle Maguire (SME)
- **Related**:
  - ADR-0002 (§04p PDF stack replaces RAGFlow)
  - `docs/architecture/reranker_v1_blockers.md`
  - `docs/architecture/parent_child_chunker_spec.md` (§1b)
  - `src/dagster/georag_dagster/assets/index_reports.py` (current Qdrant feeder)
  - `src/dagster/georag_dagster/assets/reranker_labels.py` (current
    reranker chunk-population reader)
  - `OVERNIGHT_LOG.md` §37 (§6a closure) and §38 (when written, the
    consolidation execution)

## Context

The §5e reranker LoRA pre-flight (2026-05-29 this session) surfaced a
content-corpus topology mismatch that blocks all retrieval-quality
work: there are **three** silver-tier tables that hold post-ingest
chunked text, and they disagree on which is canonical.

| Source | Rows / coverage | Backs Qdrant? | Owner asset |
|---|---|---|---|
| `silver.reports.sections_text` (JSONB sections per report) | 86 of 1,211 reports have content (7%) | ✅ `georag_reports` (15,413 points) | `index_reports` |
| `silver.document_passages` (§1b parent-child chunker output) | 7,065 rows / 1,159 docs ≥200 chars | ❌ `georag_chunks` is **empty** | `silver_parent_child_chunker` (this session) |
| `silver.ingest_extractions` (§04p PDF stack) | 246 rows / 51 ≥200 chars / 3 docs | ❌ | `silver_ingest_extractions` (ADR-0002 era) |

The reranker-label asset chain reads `silver.ingest_extractions` —
which has 47× less content than `document_passages` and is effectively
abandoned. Materialising the chain against it would produce a useless
training set. The 2026-05-19 dataset (905 labelled rows) was generated
when `ingest_extractions` was still populated; ingest output has since
moved.

Qdrant `georag_reports` is populated from `sections_text` via the
`index_reports` asset, which does its own sliding-window chunking
(MAX_CHUNK_CHARS=1500, CHUNK_OVERLAP_CHARS=200) and uses
`_deterministic_point_id(report_id, section_key)` as the point UUID.
The reranker's hard-negative miner queries Qdrant for top-k candidates,
so the point IDs must align with whatever chunk IDs the asset chain
emits. With three corpus sources and one Qdrant collection, the
alignment is currently broken.

## Decision

**`silver.document_passages` is the canonical chunked-content table
going forward.** All downstream retrieval / training / display
surfaces read from it. The other two sources are deprecated:

- `silver.reports.sections_text` becomes a **legacy column**: kept on
  disk to avoid a destructive migration, but no new code reads from it
  after this ADR. The 86 reports already chunked into Qdrant via
  `index_reports` stay in place until the backfill (below) completes,
  then `georag_reports` is dropped.
- `silver.ingest_extractions` and `silver.ingest_ocr_results` (the
  §04p PDF stack output) remain populated by the ingest pipeline as
  the **raw extraction layer** — they're the input to the parent-child
  chunker that produces `document_passages`. They are NOT read by
  retrieval / training code.

### Why document_passages

1. **Coverage**: 7,065 rows across 1,159 documents at the time of this
   ADR, versus 86 documents in `sections_text` and 3 in
   `ingest_extractions`. Order-of-magnitude difference.
2. **Provenance fields**: `document_passages` carries `page_first`,
   `page_last`, `bbox_x0/y0/x1/y1`, `parser_confidence`,
   `ocr_confidence`, `ocr_method`, `ocr_status`, `chunk_kind`,
   `parent_chunk_id`, `revision_number`, `text_hash`. These are the
   citation-precision fields §04i needs. `sections_text` carries none
   of them.
3. **Parent-child structure**: §1b parent-child chunker writes parent
   chunks (`chunk_kind='section'`) and child chunks
   (`chunk_kind='paragraph'`) with FK linkage via `parent_chunk_id`.
   §3d parent-expansion at query time needs this structure — it
   doesn't exist in `sections_text`.
4. **Multi-tenancy**: `document_passages.workspace_id` is NOT NULL and
   RLS-enforced. `sections_text` JSONB inherits tenancy via the report
   row but has no per-chunk workspace stamp.
5. **Single source of truth**: every chunk addressable by a single
   UUID (`passage_id`) — no `(report_id, section_key)` indirection.

### What's needed to execute

Multi-session work; estimated 10-15h across these focused sessions.

#### Session A — Qdrant georag_chunks index asset (~5-6h + ~2h GPU backfill)

1. New Dagster asset `index_document_passages` that:
   - Reads from `silver.document_passages`
   - Embeds `text` with `BAAI/bge-small-en-v1.5` (same model as
     `index_reports` so the vector space is shared)
   - Generates SPLADE++ sparse vectors (same model as `index_reports`)
   - Upserts into Qdrant collection `georag_chunks` with point_id =
     `passage_id` (no derivation — use the natural UUID)
   - Payload includes: `passage_id`, `document_id`, `workspace_id`,
     `chunk_kind`, `page_first`, `page_last`, `bbox_*`,
     `parser_confidence`, `ocr_confidence`, `text_hash`,
     `parent_chunk_id`, `text` (full content, no truncation snippet —
     the whole point of switching to chunks)
2. Migrate Qdrant `georag_chunks` collection definition to match the
   payload schema + create payload indices on workspace_id +
   document_id + chunk_kind
3. Backfill: run the new asset against all 7,065 existing
   `document_passages` rows. ~2h GPU on the dev A4500
4. Verify `georag_chunks.points_count` matches `silver.document_passages`
   row count + spot-check 10 random points for payload completeness
5. Update FastAPI `search_documents` tool to default to `georag_chunks`
   (gate behind feature flag `RETRIEVAL_USE_DOCUMENT_PASSAGES=true`,
   default off until the eval pass)

#### Session B — Reranker chain refactor (~3-4h)

1. Update `reranker_chunk_population` SQL to read from
   `silver.document_passages` instead of `silver.ingest_extractions` +
   `silver.ingest_ocr_results`. Map fields:
   - `passage_id` → `chunk_id` (use directly, no derivation)
   - `document_id` → `report_id`
   - `page_first` → `page`
   - `ordinal` → `region`
   - `text` → `chunk_text`
   - `chunk_kind` → derive `source_method_bucket` ('table' →
     "table-extract", 'narrative'/'section'/'paragraph' → "text",
     fallback to "text")
   - `parser_confidence` → `extraction_confidence`
   - `bbox_x0/y0/x1/y1` → `bbox` (assemble into [x0,y0,x1,y1] array)
2. Update `reranker_mined_negatives` to query `georag_chunks` instead
   of `georag_reports`
3. Update tests: the `test_reranker_labels_schema.py` invariants stay;
   the production-code patches need new fixture rows shaped against
   `document_passages`
4. Pin in a new test (`test_reranker_uses_document_passages_canonical`)
   that the SQL targets `silver.document_passages`

#### Session C — Eval pass + flip (~2-3h)

1. Run golden_queries benchmark against `RETRIEVAL_USE_DOCUMENT_PASSAGES=true`
   vs the current default. Compare NDCG@10, MRR@10, citation precision
2. If neutral or better: flip the flag default + retire `index_reports`
   asset registration + drop `georag_reports` collection
3. Document the cutover in `OVERNIGHT_LOG.md` + bump
   `parent_child_chunker_spec.md` to "canonical, post-cutover"

#### Session D — §5e XL training (the original blocker resumes)

Once A + B + C land, the reranker chain has aligned source + sink,
and the §5e LoRA training can proceed against a real 6,893-chunk
corpus. The pre-flight work from this session (locked decisions,
diagnostic verdict, `fit()` wired) carries over unchanged.

## Consequences

### Positive

- Single source of truth for chunked content — no more "which table do
  I read?" decisions in new code
- 47× larger corpus available for training + retrieval + cross-encoder
  reranking. Bumps the reranker-training surface from "unusable" to
  "realistic"
- Citation precision improves because `document_passages` carries the
  page + bbox + parser_confidence fields that `sections_text` lacks
- The parent-child structure is finally usable end-to-end — §3d
  parent-expansion was shipped against `document_passages` but
  retrieval still reads `georag_reports` which has no parent linkage

### Negative

- **The cutover is multi-session work** (~10-15h not counting the §5e
  XL itself). Cannot be done as part of a single focused-session task
- `georag_reports` (15K Qdrant points) becomes dead weight until
  formally dropped in Session C. ~50 MB Qdrant disk + RAM during the
  transition
- Code paths reading `sections_text` JSONB elsewhere (e.g. report
  display views) need an audit. **Audit deferred to Session A scoping**
- The §5e reranker XL training is blocked for another 2-3 sessions
  before it can productively start. Original 2-3 day estimate now
  effectively 5+ days

### Neutral

- `silver.ingest_extractions` / `silver.ingest_ocr_results` stay as
  raw extraction tables — they're the input to the chunker, not the
  output. No migration needed on those
- `silver.reports.sections_text` column stays on disk indefinitely.
  Not destructive; not actively read after Session C

## Open questions for Kyle (not blocking — surface during Session A)

1. After cutover, drop `georag_reports` Qdrant collection, or keep as
   read-only fallback for legacy compatibility?
2. Is there value in a one-time backfill from `sections_text` → 
   `document_passages` to capture the 86 reports that have
   `sections_text` content but no `document_passages` rows? Or just
   accept those reports are "legacy" and re-ingest them through the
   §04p stack to land them in `document_passages`?
3. Cross-corpus query views: the agentic-retrieval graph currently has
   one search tool (`search_documents`) — does it need a transition
   mode that queries both `georag_reports` and `georag_chunks` during
   the cutover, or is a hard flag flip acceptable?

## References

- §5e blockers doc: `docs/architecture/reranker_v1_blockers.md`
- §1b chunker spec: `docs/architecture/parent_child_chunker_spec.md`
- §3d parent expansion: implemented 2026-05-27 in
  `src/fastapi/app/services/retrieval/parent_expansion.py`
- ADR-0002 — the §04p stack establishes `ingest_extractions` as the
  RAW extraction layer (this ADR doesn't contradict that; it clarifies
  that ingest_extractions is upstream of, not equivalent to, the
  chunked-content layer)
- ADR-0008 — `BAAI/bge-small-en-v1.5` confirmed as the embedding model
  (this ADR adopts it for the new `index_document_passages` asset
  without re-deciding)
