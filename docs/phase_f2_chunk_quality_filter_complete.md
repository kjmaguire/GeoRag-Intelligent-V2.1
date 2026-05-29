# Phase F.2 — OCR chunk-quality filter (doc-phase 187)

**Status:** Live + filter wired into `tiff_ocr_ingester` + 21/21 ingest tests PASS + 121/121 substrate verifier.

## TL;DR — different finding than expected

Going in, I hypothesized that filtering high-noise OCR chunks would let
narrative pages rank higher in reranker scoring for the "type of uranium
deposit" question.

**What I found instead:** the *entire* Cameco WSGS corpus is uniformly
tabular. Empirical analysis of all 1,105 OCR'd Cameco passages:

```
Metric           min     p25     p50     p75     max
alpha_ratio      0.401   0.566   0.722   0.752   0.805
num_ratio        0.001   0.010   0.014   0.154   0.274
alpha_word_ratio 0.176   0.419   0.574   0.633   0.732
stopword_ratio   0.000   0.000   0.002   0.005   0.078  ← HUGE signal
vocab_size       23      74      275     455     2340
word_count       54      290     594     816     4776
```

**The `stopword_ratio` max is 0.078** — meaning the chunk in the entire
corpus with the most English stopwords still has less than 8% function
words. Narrative English prose is typically 15-25% stopwords. The
"high-quality" examples I sampled (top 3 by stopword ratio) are all
gamma-log form headers like:

```
PLAN VIEW COMPU-LOG DEVIATION CLIENT: CAMECO, USA SCALE: 2 FT/IN
LOCATION: SHIRLEY BASIN TRUE DEPTH: 397.48 FT HOLE ID: 5005-3960
AZIMUTH: 359.2 DATE OF LOG: 08/12/11 ...
```

Aggressive filtering would delete ALL 1,105 OCR passages. There's
nothing of better narrative quality to surface in their place.

## What did land

A configurable chunk-quality filter in `tiff_ocr_ingester.py`. It's
**off by default** for the Cameco corpus (where filtering would empty
the corpus) and **operator-tunable** via env vars for future ingests
where narrative content is expected.

```python
FILTER_MIN_STOPWORD_RATIO = float(
    os.environ.get("OCR_FILTER_MIN_STOPWORD_RATIO", "0.0")  # default off
)
FILTER_MIN_VOCAB_SIZE = int(
    os.environ.get("OCR_FILTER_MIN_VOCAB_SIZE", "20")  # mild default
)
```

Recommended values:
- **Tabular-heavy archives** (WSGS Cameco, gamma logs): `0.0 / 20` (default)
- **Mixed corpora** (NI 43-101 reports + log scans): `0.05 / 50`
- **Narrative-only** (technical reports, conference papers): `0.15 / 80`

The filter applies at OCR ingest time so new clusters can get
narrative-aware filtering without affecting the existing Cameco
passages.

## Files modified

- `src/fastapi/app/services/ingest/tiff_ocr_ingester.py` — added
  `_chunk_quality_passes_filter()` + integrated into `ingest_tiff_file`
- `src/fastapi/tests/test_ingest_ingesters.py` — 3 new tests
  (21/21 PASS total)

## Tests added (3)

| Test | Verifies |
|---|---|
| `test_chunk_quality_filter_accepts_narrative` | Narrative English text with default thresholds passes |
| `test_chunk_quality_filter_rejects_low_vocab` | Chunks below FILTER_MIN_VOCAB_SIZE rejected with `vocab_too_small` reason |
| `test_chunk_quality_filter_stopword_threshold_env_driven` | Raising stopword threshold rejects tabular text with `stopword_ratio_low` reason |

## Why this matters even though eval didn't move

The filter is a **forward-protection** mechanism. When (not if) Kyle
ingests narrative content — NI 43-101 reports, conference proceedings,
SME-authored technical memos — the filter will rank-protect the
genuinely retrievable narrative content from tabular OCR noise.

For the Cameco corpus specifically, the "type of uranium deposit"
question Layer 1 failure traces to a different architectural gap:

  **The deposit-type information exists in the Neo4j KG** (we put it
  there as a `:Deposit{name='sandstone-hosted roll-front uranium'}`
  node in doc-phase 180), but the orchestrator's `search_documents`
  tool only queries Qdrant. It doesn't query Neo4j for `:Deposit`
  nodes scoped to the project.

The fix is **Phase F.3: KG-aware retrieval** — extend the tool fanout
in `run_deterministic_rag` to also query Neo4j for deposit/formation
nodes when the question targets that entity class. Different
architectural work, not a filter change.

## Cumulative state

- **Doc-phase ticks this run:** **55** (132 → 187)
- **Substrate verifier:** **121/121** PASS
- **Pytest cases:** 327 → **330** (3 new chunk-quality cases)
- **Cameco corpus chunks (unchanged):** 1,108
- **Eval pass rate:** still 6/10 (no change — filter is default-off for this corpus)

## What's next

The honest hierarchy of "what would actually move the eval needle":

1. **Phase F.3 — KG-aware retrieval** (largest unlock): wire the
   orchestrator to query Neo4j `:Deposit` / `:Formation` nodes when
   the question targets that entity class. Would resolve the
   deposit-type Layer 1 fail. ~2-3 ticks.

2. **Phase F.4 — Structured-silver tool wiring**: query
   `silver.well_log_curves` directly when the question is "does this
   dataset include grade measurements". Today retrieval only finds
   text passages; structured-data answers require the orchestrator to
   know which tool to invoke. Would resolve 3 over-refusals. ~3-5 ticks.

3. **Phase F.5 — SME question refinement** (smallest tick): accept
   SQL-derived evidence for measurement-presence questions. Would
   recharacterise some failures as passes. ~1 tick but borderline
   gaming the metric.

## Honest assessment

Phase F.2 made the platform more robust for future ingests, but it
didn't move the eval because the Cameco corpus has no narrative
content to filter UP. The 6/10 ceiling persists. The fixes needed
are architectural (KG-aware retrieval, structured-silver tool
wiring), not chunk-level.
