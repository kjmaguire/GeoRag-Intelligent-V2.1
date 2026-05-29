# ADR 0008: Embedding model evaluation — what to do about `bge-small`

- **Date**: 2026-05-26 (drafted) / 2026-05-27 (accepted)
- **Status**: **Accepted — Option D (domain-fine-tune `bge-small` in place, 384-dim)**
- **Authors**: Claude Code (overnight autonomous run)
- **Deciders**: Kyle Maguire (SME)
- **Supersedes**: nothing on embedding choice
- **Related**: `docs/adr/0003-defer-v2m3-gpu-reranker.md`, `src/fastapi/app/services/ingest/passage_embedder.py`, plan §0a

## Context

Plan §0a flags the embedding model choice as the single highest-leverage unresolved item: *"Every retrieval benchmark in the plan is invalid if the embedding model changes afterward. `bge-small-en-v1.5` at 384 dimensions is a lightweight general-purpose model. Geological terminology is substantially out-of-distribution for it. This is the single biggest driver of incomplete retrieval."*

This ADR exists because **every Phase 2-5 retrieval benchmark needs a stable embedding choice first**. The plan's acceptance criterion is verbatim: *"ADR written and approved before any Phase 2 retrieval ticket begins."*

### What we run today

- **Dense:** `BAAI/bge-small-en-v1.5`, 384-dim, normalized (`passage_embedder.py:8`).
- **Sparse:** SPLADE++ named vector `"text"` in the same Qdrant collection (`passage_embedder.py:10-17`).
- **Collection:** `georag_reports` — `{'': VectorParams(size=384, distance=Cosine)}` + `{'text': SparseVectorParams(...)}`.
- **Reranker:** `bge-reranker-base` CPU (ADR-0003 §1); domain-fine-tune in flight (`project_reranker_v1.md`).

So the "plan's option A" (keep bge-small + hybrid BM25) is **already partially shipped**: the SPLADE sparse vector already lives alongside the dense vector, so the sparse-retrieval bones are there even though plan §2d's "BM25" framing is technically Splade-not-BM25.

### What plan §0a asks us to evaluate

Five options with the dimensions/VRAM/recall tradeoffs listed in plan §0a Table A:

1. **A.** Keep `bge-small` + hybrid (sparse + dense) — no re-index.
2. **B.** Upgrade to `bge-base-en-v1.5` (768-dim) — full re-index.
3. **C.** Upgrade to `bge-large-en-v1.5` (1024-dim) — full re-index.
4. **D.** Domain fine-tune `bge-small` on the 150 GB corpus — full re-index, same 384-dim.
5. **E.** Domain fine-tune `bge-base` on the 150 GB corpus — full re-index, 768-dim.

### What plan §0a leaves open

The plan deliberately *does not* pick a winner. The acceptance criterion is "ADR written and approved" — meaning Kyle decides after seeing the tradeoffs in context. This ADR's job is to surface the decision-relevant facts, not pre-commit.

### Cross-cutting facts that constrain the choice

1. **Reranker fine-tune (ADR-0003 / v1) is already domain-aware.** If the reranker absorbs most of the domain-vocabulary gap, the embedding-model upgrade leverage shrinks. Plan §0a assumes the embedding is the dominant lever; that assumption is testable once reranker-v1 lands.
2. **Re-index cost is non-trivial.** `silver.document_passages` is in the millions of rows after 2026-04 ingest waves; re-embedding everything is hours of GPU time. Plan §2b sets `--gpu-memory-utilization` to 0.85 (from 0.92); current memory `project_gpu_acceleration_2026_05_22.md` documents 0.80 ceiling. Re-embedding on the A4500 alongside vLLM is feasible but slow.
3. **Qdrant collection dimension is fixed at create-time.** Going from 384 → 768 requires either a NEW collection (write to both during transition) or a destructive recreate. Either way, payload + ID stability has to be preserved.
4. **Sparse vector already exists and is independent of dense dim.** Switching dense models does NOT require re-encoding sparse. The hybrid query path stays.
5. **Fine-tuning bge-small on the 150 GB corpus needs labelled positives.** The plan implies "use the corpus" but doesn't specify the label set. Realistic candidates: synthetic queries generated the same way as reranker-v1 (Qwen3 generates queries from chunks → positive pair), historical user queries from `answer_runs`, or curated SME pairs. None of these are free.

## Decision required

**This ADR does not pre-decide.** It frames the call so Kyle can make it in one pass in the morning.

### Recommended path (subject to Kyle's override)

> **Option D — Domain fine-tune `bge-small` in place.**

Rationale, in priority order:

1. **Dimension stability** — keeps Qdrant collection schema unchanged. Existing payload, IDs, sparse vectors, indexes all unaffected. Re-index can be done as a rolling backfill rather than a destructive recreate.
2. **Reuses the reranker-v1 label pipeline.** The same synthetic-positive generation Qwen3 is doing for reranker labels (`project_reranker_v1.md`) can mint positive pairs for embedding contrastive fine-tuning. Marginal infrastructure cost is small.
3. **Avoids coupling with the reranker decision.** If we move dense to bge-base (option B) at the same time we're fine-tuning the reranker, attribution of recall lift becomes ambiguous. Keep dim stable; let the reranker upgrade prove its own lift first.
4. **Hardware-fit.** Fine-tuning bge-small on A4500 with LoRA r=16 fits in memory comfortably; bge-base fits but with less headroom; bge-large would compete with vLLM for VRAM. Plan §0a's "GPU for training only" claim for D matches reality on the A4500.
5. **Reversible.** If domain-fine-tuned bge-small fails to lift recall@20 by ≥5 points on the golden question set after re-index, we have the option-B (upgrade to bge-base) lever still available. Doing B first forecloses the chance to evaluate D cleanly.

### Decision matrix

| Option | Re-index cost | Schema change | Eval-isolation from reranker work | Recall ceiling | Reversibility |
|---|---|---|---|---|---|
| A. Keep + hybrid | None | None | High (no change) | Low | n/a (baseline) |
| B. Upgrade to bge-base 768-dim | High (full) | YES (collection recreate) | Low (entangled with reranker-v1 timing) | Higher | Low (re-index again to revert) |
| C. Upgrade to bge-large 1024-dim | Very high (full) | YES | Low | Highest (generic) | Low |
| D. Fine-tune bge-small 384-dim | High (full) but rolling | None | Medium (label pipeline shared with reranker) | High for domain | Medium |
| E. Fine-tune bge-base 768-dim | Very high | YES | Low | Highest for domain | Low |

### Trigger conditions to reopen this ADR

Reopen and consider option B or E if **all** of:

1. Option D re-index has completed.
2. Reranker-v1 has been promoted (or rejected) per ADR-0003 trigger conditions.
3. Recall@20 on the golden question set is < 0.85 (plan §Success Definition).
4. The per-slice gap analysis (golden question set categories — plan §5a §20 categories) shows recall failures concentrated in **conceptual** queries rather than **exact-term** queries. (Conceptual gap → bigger dense encoder is the right fix; exact-term gap → sparse/SPLADE tuning is the right fix.)

## Consequences

### If Kyle accepts D (recommended)

1. **Phase 0a is complete** — ADR-0008 status flips Proposed → Accepted.
2. **Baseline measurement** runs against current production stack (`bge-small` stock) on the golden question set (plan §5a) to fix the bge-small reference number. Plan §0a explicitly requires this baseline before any change.
3. **Label pipeline reuse from reranker-v1** — extend `src/dagster/georag_dagster/assets/reranker_labels.py` (or sibling asset) to emit contrastive embedding pairs alongside the reranker training pairs.
4. **Fine-tune script** — new `src/fastapi/scripts/finetune_bge_small.py` modelled on the reranker LoRA script (`a593037` in git log).
5. **Rolling re-index** — backfill embeddings in batches; flip a `EMBEDDING_VERSION` payload field so the retrieval path can prefer new-version embeddings during transition.
6. **Re-baseline** on the golden set with the fine-tuned model; compare to step 2.

### If Kyle picks B or E (dimension change)

Add to the above:

7. **New Qdrant collection** `georag_reports_v2` with 768-dim dense + same sparse.
8. **Dual-write** during transition (`passage_embedder.py` writes both collections).
9. **Cutover plan** — read path queries new collection only after backfill is ≥95% complete.
10. **Reranker-v1 promotion gate** must NOT fire during the embedding transition (entangles the variables).
11. **Payload ID strategy** — keep point IDs deterministic across collections (passage row PK) so backfill is idempotent.

### If Kyle picks A (no change)

- Lock the current `bge-small` + SPLADE hybrid as canonical.
- Phase 2a (retrieval-K decouple), Phase 2d (hybrid retrieval — already 90% there), Phase 3c (aggregate reranking) all proceed against the current embedding stack.
- This ADR closes as **Rejected — baseline is fit for purpose**; reopen only if golden-set recall fails the §Success Definition threshold.

## Decision record — 2026-05-27 morning

Kyle reviewed and accepted Option D with the following sub-decisions:

| Q | Question | Decision |
|---|---|---|
| Q25 | Embedding option A / B / C / D / E? | **D** — fine-tune `bge-small` in place, 384-dim |
| Q26 | Measure `bge-small` baseline before flipping ADR, or as step 1 of D? | **Step 1 of D** — golden set still has placeholders; partial baseline today buys little |
| Q27 | Reranker-v1 promotion ahead of or after embedding fine-tune? | **Reranker-v1 first** — clean evidence attribution before stacking a second variable |
| Q28 | Training corpus — full 150 GB or curated subset? | **Curated subset for v1**: NI 43-101s + assay tables + lithology logs. Full corpus only if v1 underperforms. |
| Q29 | Recall@20 target — full golden set or domain subset? | **Full golden set + per-category breakouts**, so we can see where the lift concentrates |

### Rationale shift documented

Kyle correctly noted that he intends to delete + re-ingest the corpus, which neutralises one of the five original reasons for picking D (dimension stability — re-ingest pays the schema change for free anyway). The other four reasons still hold:

1. ~~Dimension stability~~ — neutralised by planned re-ingest.
2. Reuses reranker-v1 label pipeline — still applies.
3. Eval-isolated from reranker-v1 upgrade — still applies (one axis of change at a time).
4. Hardware-fits A4500 with LoRA — still applies.
5. Reversible — option B/E still available if D's recall lift < 5pp on the golden set.

The decisive factor remaining: **a bigger generic encoder (B) doesn't fix out-of-distribution vocabulary**; only domain fine-tuning does. Plan §0a's own framing supports this: *"Domain fine-tune bge-small on 150GB corpus → Highest for domain terms."*

The "free re-index" credit the planned re-ingest creates is being spent on plan §1b chunking, §1c classification, §1d CGI vocab tagging, and §1e structured geological data extraction — not on a bigger embedding model.

### Next concrete steps (now unblocked)

1. **Run §5a baseline** — once Q14 (seeder authored_by_user_id) is resolved and Q15 SME expansion pass lands, run the golden set against current production stack to fix the `bge-small` reference number.
2. **Extend `src/dagster/georag_dagster/assets/reranker_labels.py`** (or sibling asset) to emit contrastive embedding pairs alongside the reranker training pairs.
3. **Build `src/fastapi/scripts/finetune_bge_small.py`** modelled on the reranker LoRA script (commit `a593037`).
4. **Rolling re-index** at the next planned re-ingest — flip `EMBEDDING_VERSION` payload field for transition.
5. **Re-baseline** against golden set; compare to step 1.

### Trigger conditions to reopen this ADR

(Unchanged from original draft, abbreviated.) Reopen to consider B or E if **all** of:

1. Option D re-index has completed.
2. Reranker-v1 has been promoted (or rejected) per ADR-0003 trigger conditions.
3. Recall@20 on the golden question set is < 0.85 (plan §Success Definition).
4. Per-category gap analysis shows recall failures concentrated in **conceptual** queries rather than **exact-term** queries.

## References

- Plan §0a (the question this ADR answers)
- Plan §2d (hybrid retrieval — already mostly shipped via SPLADE)
- Plan §5a (golden question set — needed to baseline)
- Plan §Success Definition (recall@20 ≥ 0.85)
- ADR-0003 (reranker decision precedent — same fine-tune-in-place logic argued)
- `src/fastapi/app/services/ingest/passage_embedder.py` (current dense + sparse encoder)
- `project_reranker_v1.md` (reranker label pipeline this ADR proposes to reuse)
- `project_gpu_acceleration_2026_05_22.md` (A4500 VRAM headroom constraints)
