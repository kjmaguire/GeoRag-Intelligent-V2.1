# ADR-0012: Structured-to-NL summary corpus expansion

| Status     | Proposed                                              |
|------------|-------------------------------------------------------|
| Date       | 2026-05-28                                            |
| Deciders   | Kyle (SME)                                            |
| Supersedes | —                                                     |
| Related    | ADR-0010 (silver.document_passages canonical corpus); ADR-0011 (reranker domain adaptation) |

## Context

The 2026-05-28 LoRA reranker bench failed the OOD gate with a -17.1pp
collapse on the `core_chat` slice — the synthetic training distribution
didn't match the conversational query distribution geologists actually
ask. Post-mortem (see `models/reranker/.../OOD_HOLD_VERDICT.md`) flagged
the root cause as a distribution mismatch.

Investigation revealed a deeper structural gap: **the canonical corpus
(`silver.document_passages`) only contains prose from PDFs — NI 43-101
reports and now the Earle textbook**. Every other silver table holds
the actual structured geological data (assays, lithology, collars,
samples, structures, LAS curves, field observations, public geoscience
records) — none of which exist as text-retrievable chunks.

Concrete consequence: when a geologist asks *"what was the U₃O₈ in
PLS-22-11 around 142m?"*, the chat pipeline can answer via the
`query_assay_data` tool (returning numbers) but **no chunk surfaces in
`search_documents`** because no passage in Qdrant mentions sample IDs,
exact intervals, QA flags, or method codes. The reranker has never
seen this vocabulary in training and has no way to score relevance for
it at inference time.

For Phase 3 (full reranker FT, ADR-0011) to actually beat the stock
baseline on conversational geology questions, the training distribution
needs to *match* what geologists ask — and that requires the corpus to
include the structured-data side of the lake, not just the prose side.

## Decision

Build a new Dagster asset group `silver_nl_summaries` that synthesizes
natural-language summary passages from every meaningful structured
silver table, INSERTs them into `silver.document_passages` with a new
`chunk_kind='structured_summary'`, and lets the existing embed cron
(ADR-0010 §A) carry them into `georag_chunks` automatically.

Templates are **deterministic** — no LLM in the loop. Reasons:

* Reproducibility — same input row always produces the same passage.
* Speed — template rendering is 10⁴× faster than LLM call per row.
* Cost — no inference tokens consumed on what's a one-shot corpus build.
* Auditability — every NL claim is mechanically traceable to the
  structured row it came from, satisfying the citation-mandatory rule.

### Locked decisions

| Decision | Choice |
|---|---|
| `chunk_kind` value | `structured_summary` |
| Idempotent passage_id | `uuid5(NAMESPACE_OID, f"{source_table}:{source_row_id}")` — same row always produces same UUID |
| RLS | inherit `workspace_id` from the source row |
| Update strategy | UPSERT — re-synthesize when source row's `updated_at` > passage `updated_at` |
| Tombstone deleted source rows | NO — leave the passage; cleanup is a future ADR |
| Cross-table joins | yes, eagerly — assay passages include collar + lithology context; lithology passages include collar context |
| Template rendering format | f-strings, parameterised per source-type |
| `parser_used` field | `structured_summary_v1` for filtering / audit |

### Source coverage

Priority order, by expected per-row impact on conversational queries:

1. **`silver.assays_v2`** — highest impact. Sample IDs, intervals, QA flags, method codes, all in geologist-natural prose with the assay value inline.
2. **`silver.lithology`** — interval-based rock descriptions. Joins to collars for hole context. Includes rock_code, description, logger, date.
3. **`silver.collars`** — drillhole metadata. One per hole. Type, dip, azimuth, total depth, project context, drill date, geologist.
4. **`silver.samples`** — sample-level metadata (often referenced in assay results).
5. **`silver.structures`** — strike/dip/lineation measurements at outcrops.
6. **`silver.las_curves`** — depth-banded curve summaries (gamma, density, sonic).
7. **`silver.review_queue`** — QField field observations from geologists in the field.
8. **`silver.public_*`** — public geoscience pulls (BC MINFILE, USGS NGMDB, NRCan etc.) — re-render their structured columns as narrative.

This ADR ships #1, #2, #3 fully implemented + stubs for #4-#8 as the
incremental path. Each future stub becomes a one-or-two-day follow-up
PR with its own template + tests; no upstream-design dependencies.

## Consequences

### Positive

* The training distribution finally matches the inference distribution.
  Reranker training data + MLM corpus naturally include geologist
  vocabulary (sample IDs, hole IDs, rock codes, depths, QA codes).
* Retrieval surfaces "the row" as a chunk — `search_documents` returns
  a passage describing the specific assay/interval/hole the user asked
  about, not just nearby prose from a PDF that happened to mention the
  topic at a distance.
* Cross-source joins land in the synthesized passages where they
  belong — an assay passage that ALSO says "logged as graphitic pelitic
  gneiss" carries the lithology context inline, which is impossible to
  express in either source table alone.
* The next ADR-0011 Phase 2/3 cycle trains on the enriched ~30,000-
  passage corpus instead of the 7,929-passage prose-only one — 3-4×
  more diverse training signal.

### Negative

* Corpus size grows from ~7,900 to ~30,000 passages. Embedding cost
  scales linearly — bge-small on the A4500 handles this in minutes,
  not a real constraint.
* Synthesized passages are deterministic and template-shaped — they
  will repeat phrase patterns across many rows ("sample X from
  drillhole Y at depth Z to W..."). The reranker should pick the
  *content* signal up regardless, but per-template repetition could
  bias attention if not balanced. Watch for it in the first eval pass.
* `silver.document_passages` no longer means "PDF-only chunks". The
  `chunk_kind` filter is the discriminator — UIs that surface "report
  passages" should filter `chunk_kind='paragraph'` (the PDF default)
  vs `chunk_kind='structured_summary'`.

### Neutral

* No production code path changes from this ADR alone. The chat
  pipeline reads `georag_chunks` regardless of chunk_kind, so retrieval
  picks up both prose and structured-summary passages transparently.
  The reranker training pipeline reads `silver.document_passages`
  regardless of chunk_kind, so structured summaries flow into the
  training data automatically.

## Implementation

New module `src/dagster/georag_dagster/assets/silver_nl_summaries.py`
shipping with:

* `silver_assays_v2_nl_summary` — fully implemented
* `silver_lithology_nl_summary` — fully implemented
* `silver_collars_nl_summary` — fully implemented
* `silver_samples_nl_summary` — stub (TODO marker)
* `silver_structures_nl_summary` — stub
* `silver_las_curves_nl_summary` — stub
* `silver_review_queue_nl_summary` — stub
* `silver_public_geo_nl_summary` — stub

Each asset:

1. Reads source-table rows that have `updated_at` newer than the
   passage row's `updated_at` (or no passage exists yet).
2. Joins to related context tables eagerly (assay → collar + lithology;
   lithology → collar; etc.).
3. Renders the deterministic template, producing a 200-500 char passage.
4. UPSERTs into `silver.document_passages` with:
   * `passage_id = uuid5(NAMESPACE_OID, f"{source_table}:{source_row_id}")`
   * `workspace_id = <inherited from source row>`
   * `chunk_kind = 'structured_summary'`
   * `parser_used = 'structured_summary_v1'`
   * `revision_number = 1`
   * `text = <rendered template>`
   * `text_hash = sha256(text)[:64]`

Tests live in `src/dagster/tests/test_silver_nl_summaries.py` and cover:

* Template rendering for the three implemented synthesizers — fixed
  input rows produce fixed output strings.
* Idempotency — re-rendering the same row produces the same
  passage_id and the same text.
* Update detection — incrementing source row's `updated_at` triggers
  re-synthesis; unchanged source row skips.
* Workspace_id propagation — RLS-isolated by source row's
  workspace_id.

## Execution order

After Phase 2 MLM + Phase 3 full FT (currently in flight / queued for
this evening's GPU window):

1. Run `dagster asset materialize --select silver_assays_v2_nl_summary` →
   ~5,000 new passages.
2. Run the lithology + collars assets → ~12,000 + ~150 new passages.
3. Wait for embed cron to catch up (~10 min × however many embed
   batches the worker processes).
4. Bench the EXISTING reranker (stock or LoRA candidate) against the
   119-q golden set with the enriched corpus — measures whether
   improved retrieval alone (with the same reranker) helps.
5. Re-run vocab extraction — will surface many new domain tokens now
   that drill hole IDs + sample IDs are whole-word frequent.
6. Re-run Phase 1 (extend tokenizer) → Phase 2 (MLM) → Phase 3 (full FT)
   on the enriched corpus.
7. Bench again — this is where we expect to actually beat baseline.

The four stubs (samples / structures / las_curves / public_geo /
review_queue) each become independent follow-up PRs with their own
template + tests. No ordering dependency between them.
