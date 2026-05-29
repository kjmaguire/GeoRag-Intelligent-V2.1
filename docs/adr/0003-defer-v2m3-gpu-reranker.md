# ADR 0003: Defer bge-reranker-v2-m3 + GPU reranker host upgrade

- **Date**: 2026-05-19
- **Status**: Proposed (Deferred — trigger conditions below)
- **Deciders**: Kyle Maguire (SME)
- **Supersedes**: nothing; first ADR on reranker base architecture
- **Related**: `src/fastapi/app/services/reranker.py`, `docs/adr/0002-04p-stack-replaces-ragflow.md` (GPU contention precedent)

## Context

GeoRAG runs a cross-encoder reranker between Qdrant top-K retrieval and
LLM context assembly. The production reranker is `BAAI/bge-reranker-base`
(Apache 2.0, ~278 MB), SHA-pinned, **CPU-only**, singleton with lifespan
warmup, 2 s timeout with RRF fallback. It powers both chat retrieval and
eval Layer 5 chunk-provenance gating. Per-query-class top-k is already
tuned (factual 20, spatial 30, document 15, computation 10, viz 30,
unknown 20). The audit trail records `answer_runs.reranker_version`
(`bge-reranker-base@<sha8>`) for every answer.

Reranker v1 (the in-flight work this ADR sits alongside) does **domain
fine-tuning of `bge-reranker-base` in place** — synthetic literal +
paraphrase queries generated from the §04p PDF corpus via Qwen3-30B-A3B
on vLLM, LoRA r=16 training, promotion-gated at ≥+5pp NDCG@10 with no
per-slice regression >2pp. Multi-hop queries are deferred to v2. Base
architecture is **not** changed in v1.

An alternative was considered for v1: upgrade the base architecture to
`BAAI/bge-reranker-v2-m3` (568 MB, multilingual, stronger encoder) and
move the reranker off CPU to a GPU host (separate or colocated with
vLLM). It was rejected for v1 on these grounds:

1. **The integration and provenance plumbing for v1 already exists.**
   Spec sections covering pipeline integration and provenance
   pass-through are satisfied by the current code path
   (`src/fastapi/app/agent/tools.py:1362` — reranker sees only
   `(query, text)` tuples and returns `list[float]`; chunk objects flow
   through unchanged except for `relevance_score`). Swapping the base
   model in v1 would touch infra (GPU host), latency budget, and the
   architecture doc all at once, before any domain-fine-tune evidence
   exists.

2. **GPU contention is a known constraint** (see ADR 0002 §3). The
   A4500 has ~300 MiB–1 GiB free at idle with vLLM running at
   `--gpu-memory-utilization 0.92`. `bge-reranker-v2-m3` at ~568 MB +
   activations does not fit alongside vLLM without lowering KV-cache
   budget or adding a second GPU. Colocating is not viable at workstation
   scale; a separate GPU host is a hardware ask.

3. **The 2 s CPU budget is met today.** `bge-reranker-base` reranks
   50 candidates within budget on the Threadripper Pro 5955WX. Moving
   to a GPU is a latency improvement, not a correctness fix.

4. **Domain fine-tuning of the existing base is the higher-leverage
   first move.** Geological vocabulary (lithology, grade, cutoff,
   intercept, QA/QC) is underweighted by stock multilingual encoders.
   Fine-tuning `bge-reranker-base` on synthetic labels mined from the
   ingested NI 43-101 + drill-log + assay corpus is expected to lift
   NDCG@10 more than swapping to a larger generic encoder, at a
   fraction of the infra cost.

## Options considered

| Option | Effort | Outcome |
|---|---|---|
| A. **Defer v2-m3 + GPU upgrade until v1 evidence lands** | Low | v1 ships in days; produces synthetic-label and eval-harness infrastructure that's reusable regardless of which base model wins. Promotion-gate data becomes the evidence base for or against the v2-m3 upgrade. **Chosen.** |
| B. Upgrade to v2-m3 + GPU host in v1, fine-tune on the new base | High | Best-case quality ceiling, but couples a hardware ask, a latency-budget renegotiation, and an architecture-doc update to the same change set. Rejected on scope discipline grounds and on GPU-contention precedent (ADR 0002 §3). |
| C. Upgrade to v2-m3 on CPU (no GPU move) | Medium | v2-m3 at 568 MB doubles CPU rerank latency on the existing Threadripper; would breach the 2 s budget at k=50. Rejected on latency grounds. |
| D. Skip the reranker entirely; rely on Qdrant ANN | Low | Loses the precision gate; degrades Layer 1 of §04i hallucination prevention. Rejected outright. |

## Decision

**Defer the v2-m3 + GPU host upgrade.** Reopen this ADR for a
revisit-and-decide pass when **all** of the following trigger
conditions hold:

1. **v1 promotion-gate data exists.** `services/eval/retrieval_metrics/`
   has run pipeline (A) `Qdrant top-50 → stock bge-reranker-base` and
   pipeline (B) `Qdrant top-50 → fine-tuned bge-reranker-base` against a
   held-out test split, and `assess_retrieval_promotion()` has produced
   a verdict (pass or fail).
2. **The v1 fine-tune outcome is known.** Either:
   - **v1 passed the promotion gate** → reopen this ADR to ask whether
     the larger v2-m3 base can lift NDCG@10 further on the same eval
     harness; budget the upgrade against the marginal lift.
   - **v1 failed the promotion gate** → reopen this ADR to ask whether
     the failure is base-architecture-bound (v2-m3 likely to fix) or
     label-quality-bound (more labels / multi-hop / SME review more
     likely to fix). Do not assume the upgrade is the answer.
3. **GPU budget is available.** Either a second GPU has been
   provisioned, or vLLM has shifted to a smaller model that frees
   ≥1 GiB on the A4500 at idle, or ADR 0002 §3 is revisited with
   updated measurements.

## Consequences

### What stays the same (until trigger conditions hold)

- `BAAI/bge-reranker-base` remains the production reranker.
- CPU-only deployment, 2 s timeout, RRF fallback on timeout/failure.
- Per-query-class top-k values unchanged.
- `RERANKER_VERSION` string format unchanged: `bge-reranker-base@<sha8>`.
  v1 fine-tune flips this to `bge-reranker-base@georag-<date>-<sha8>` so
  the audit trail distinguishes domain-fine-tuned from stock weights.

### What this ADR enables

- The synthetic-label generation asset (`reranker_chunk_population` →
  `reranker_label_dataset` in `src/dagster/georag_dagster/`) and the
  retrieval-metrics eval harness (`src/fastapi/app/services/eval/retrieval_metrics/`)
  are designed to be base-model-agnostic. The same label set and the
  same harness will train and evaluate v2-m3 if the trigger conditions
  fire.

### What this ADR closes off

- Nothing irreversibly. The v1 fine-tuned LoRA adapter sits at
  `models/reranker/georag-bge-base-<date>-<sha>/`; reverting to stock is
  a config flip. Upgrading to v2-m3 later is a base-model swap plus a
  new fine-tune run on the same labels.

### Open questions to answer at revisit

- Marginal NDCG@10 lift from v2-m3 over fine-tuned base on the same
  test split — is the lift larger than the per-slice variance?
- Latency budget under GPU: target p95 < 150 ms at k=50 vs current 2 s
  CPU envelope. Does the orchestrator need a wider top-K (50 → 100) to
  exploit the cheaper compute?
- DevOps cost of a separate reranker GPU host vs colocation with vLLM
  on a future second GPU.
- Whether the v2-m3 multilingual encoder helps or hurts on the
  English-only NI 43-101 corpus.

## References

- v1 work in flight: synthetic labels asset
  (`src/dagster/georag_dagster/assets/reranker_labels.py`), eval harness
  (`src/fastapi/app/services/eval/retrieval_metrics/`), LoRA fine-tune
  script (TBD; depends on label asset landing).
- Memory record: `project_reranker_v1.md` (Kyle's `.claude/memory/`).
- GPU contention precedent: `docs/adr/0002-04p-stack-replaces-ragflow.md` §3.
- Reranker module + version contract: `src/fastapi/app/services/reranker.py`.
- Provenance pass-through verification: `src/fastapi/app/agent/tools.py:1362`.
