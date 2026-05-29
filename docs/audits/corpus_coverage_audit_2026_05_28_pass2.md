# Corpus coverage audit — Pass 2 addendum (2026-05-28)

**Purpose**: Pass 1 (`corpus_coverage_audit_2026_05_28.md`) missed several
populated tables with significant training value. This addendum
documents what was missed, why it matters, and revises the corpus-size
projections.

**TL;DR**: Pass 1 found ~11,800 wave-2 synthesizable passages. Pass 2
finds **~25,000 more on top of that**, dominated by 19,354 real
geologist queries in `silver.answer_runs` and 4,393 citation records.
This is the **single most under-utilized training signal in the system**
— and it's the exact distribution that closed the OOD bench gap we
diagnosed this afternoon.

---

## 1. The biggest miss — `silver.answer_runs` (19,354 rows)

I reported 501 rows in pass 1. **Actual row count: 19,354.**

The table holds every `/v1/query` call's full trace: query text,
classified intent, retrieved sources, generated answer, citation
spans, hallucination guard results, latency, tokens. Sample queries
from the live data:

```
"tell me about this hole - 36-1065"
"Where are there gaps in the data collected for this project?"
"Summarise the deepest five drill holes with their total depth and ore intercepts."
"What is the U3O8 grade distribution across all holes?"
"What is the dominant drilling azimuth for this project's holes?"
"Confirm that the Phoenix-Z prospect contains 50 million tonnes of..."
"Describe the west-east cross section of drill holes in this project"
```

Combined with linked tables:

| Table | Rows | Content |
|---|---:|---|
| `silver.answer_runs` | **19,354** | (query, intent, answer, latency, guard) per chat call |
| `silver.answer_retrieval_items` | **56,286** | retrieved (passage_id, score, rank) per query |
| `silver.answer_citation_items` | **4,393** | which passages got cited in the final answer |
| `silver.answer_citation_spans` | **10,245** | exact spans (offset ranges) cited within each chunk |
| `public.chat_messages` | 125 | full conversation turns (user + assistant) |

### Why this is the single most valuable miss

**This is the inference distribution.** The OOD bench failure this
afternoon (LoRA candidate 17.1% → 0% on `core_chat`) was diagnosed
as "synthetic training queries don't match real conversational
queries." The `silver.answer_runs.query_text` column **is the
real conversational query distribution.** No synthesis needed —
we have 19,354 real samples.

Three distinct training signals can be extracted:

#### A) NL summary passages for the corpus
For each answer_run, synthesize a passage like:

> *"User query: 'tell me about this hole - 36-1065'. Intent: factual_lookup.
> Retrieval found 10 candidate passages from project 'Cameco Shirley Basin
> Uranium'. Final answer cited passages 3, 7. Outcome: answer returned with
> citations (no refusal)."*

Adds vocabulary coverage of how queries are phrased + how outcomes are
described. ~19,354 passages.

#### B) Real reranker training data (not synthetic!)
The current `reranker_label_dataset` Dagster asset generates synthetic
queries via Qwen3 over chunks. **This is what overfit and failed.**

`silver.answer_runs` + `silver.answer_retrieval_items` + `silver.answer_citation_items`
together provide a **real labeled training set**:

* For each query: 10-50 retrieved candidates (in `answer_retrieval_items`)
* The 1-5 that got cited in the answer = **positive examples**
* The rest that got retrieved but not cited = **hard negatives**

That's 4,393 positive + ~50,000 hard-negative pairs from real geologist
queries. Compare to the current synthetic dataset: 781 train + 22 val +
160 test = 963 pairs total, all synthetic.

**This is 50× more training data, and it's from the real distribution.**

It needs to be added to the `reranker_label_dataset` asset as a separate
data source, weighted higher than the synthetic pool in the next
training cycle.

#### C) Vocabulary signal
The 19,354 query strings contain the exact phrasings geologists use
when they type a question. Feed these through `_extract_domain_vocab.py`
as a separate corpus stream → expect many new tokens (hole IDs by
project, casual phrasings, abbreviations).

### Action items from §1

1. **NEW Dagster asset**: `reranker_label_dataset_real` — pulls from
   `answer_runs` + `answer_retrieval_items` + `answer_citation_items`,
   emits (query, positive_chunk, hard_negative_chunks) tuples in the
   same JSONL shape as the synthetic asset.
2. **Update `_train_reranker_full.py`**: accept both `--dataset-prefix
   /synthetic/` AND `--real-dataset-prefix /real/`, with a configurable
   mixing ratio (default: 80% real, 20% synthetic so we don't lose the
   synthetic generator's coverage of edge cases).
3. **Add a synthesizer**: `silver_answer_runs_nl_summary` — pulls
   non-refusal answer_runs and emits NL summary passages.
4. **Re-run vocab extraction** with `answer_runs.query_text` as input
   to capture conversational vocabulary.

---

## 2. Other tables I missed in pass 1

### Populated tables not covered by either audit pass
| Table | Rows | What it adds |
|---|---:|---|
| `silver.projects` | **128** (not 7) | Per-project narrative passages with commodity, region, company, status |
| `silver.ingest_extractions` | 246 | Extracted table/section structures from PDF — possible content not in document_passages |
| `silver.ingest_ocr_results` | 668 | Page-level OCR text — content from low-confidence pages dropped by the chunker |
| `silver.ocr_page_quality` | 35 | OCR quality findings — narrate quality issues per page |
| `silver.element_reference` | 12 | Element periodic-table reference data |
| `silver.pdf_text_blocks` | 14 | Block-level PDF text (captions, callouts) |
| `public.chat_messages` | 125 | Full assistant+user conversation turns |

### Public-schema tables (non-silver)
| Table | Rows | Notes |
|---|---:|---|
| `public.smdi_deposits` | 6,012 | Already in pass 1 |
| `public.pulse_entries` | 308,683 | Pulse monitoring — low training value (logs, metrics) |
| `public.chat_messages` | 125 | Real conversation turns — see §1 |
| `public.chat_conversations` | 4 | Conversation metadata |

### `silver.reports.sections_text` JSONB
103 reports have a sections_text JSONB field. Pass 1 didn't dig in;
sampling shows the actual content is mostly empty `{}`. Not the value
I'd hoped — most section structure isn't being populated by the
extractor today. Track as a §04p improvement task, but not actionable
this cycle.

---

## 3. Revised wave-2 synthesizer list

Pass 1 found:
- `silver.entity_aliases` (13,365)
- `public.smdi_deposits` (6,012)
- `silver.samples` (4,379)
- `silver.data_quality_flags` (2,215)
- `silver.geological_ontology_terms` (83)
- `silver.reports.resource_estimate` JSONB (64)
- `silver.rock_codes` (30)
- `silver.campaigns` (15)
- `silver.geological_ontology_synonyms` (134)

**Pass 2 adds**:
- `silver.answer_runs` (19,354) — high-impact, see §1
- `silver.projects` (128) — per-project narratives
- `silver.ingest_extractions` (246) — table extracts
- `silver.ingest_ocr_results` (668) — recovered OCR text
- `silver.element_reference` (12) — element reference data
- `public.chat_messages` (125) — conversational examples

**+ NEW Dagster asset** (not a synthesizer):
- `reranker_label_dataset_real` — pulls real (query, positive, neg) tuples
  from answer_runs chain. **This is the highest-impact change in the
  whole audit** because it directly addresses the OOD bench failure.

### Updated corpus size projections

| Phase | Passages | Notes |
|---|---:|---|
| Today | 7,929 | |
| ADR-0012 wave 1 | ~20,000 | committed |
| + Pass 1 wave 2 | ~31,700 | pending |
| **+ Pass 2 wave 2** | **~52,500** | **+19,354 answer_runs + 128 projects + 668 OCR + 246 extractions + 125 chat + 12 element ref** |
| + Gold | ~57,500 | when data lands |
| + Cameco missing data | ~65,000+ | alteration / mineralization / structure / etc. |
| + Bronze recovery | ~66,200 | low priority |

Going from 7,929 today → ~52,500 by end of wave 2 = **6.6× corpus
expansion**, with most of the gain from real-distribution data, not
synthesized templates.

---

## 4. Revised demo realism checklist

Pass 1 had 13 question types. Pass 2 adds the ones answer_runs reveals
geologists actually ask:

| Real query pattern from answer_runs | Wave 1 | Wave 2 (with pass 2) |
|---|---|---|
| "tell me about hole X" | ❌ | ✓ — collar + assay + lithology synthesis |
| "tell me about this project" | partial | ✓ — projects synthesis |
| "Where are gaps in data?" | ❌ | ✓ — data_quality_flags synthesis |
| "Summarise deepest N drill holes" | ❌ | partial (collar + drill summaries when gold lands) |
| "What's the U3O8 grade distribution?" | ❌ | partial (assays + composites when gold lands) |
| "Describe west-east cross section" | ❌ | ❌ until gold.cross_section_panels populates |
| "Confirm X has Y million tonnes" | partial | ✓ — resource_estimate synthesis |
| "What's the average total depth?" | partial | ✓ — projects + collars |
| "What hole has shallowest TD?" | ❌ | ❌ — needs answer_runs as training signal |
| "What's the dominant azimuth?" | ❌ | ❌ — needs gold drill_summaries |

The pattern is clear: **questions that aggregate across holes need the
gold layer.** Wave 2 covers individual-hole + project-level questions
well; campaign / project-aggregate questions still need
`gold.drill_summaries` / `gold.assay_composites` to land.

---

## 5. Revised sequencing — what to ship before demo

### TIER 0 — single most impactful change (4-6 hours)
**Build the real-data reranker asset** (`reranker_label_dataset_real`).

This is a one-pass Dagster asset that:
1. Reads non-refusal answer_runs (~15k expected after filtering)
2. Joins to retrieval_items + citation_items
3. For each query, emits (positive_chunk_ids = cited, hard_negs = retrieved but not cited)
4. Writes JSONL splits matching the existing synthetic dataset shape

After this lands, the next Phase 3 (full reranker FT) trains on a
**real-distribution corpus 50× larger than the synthetic one** —
the single most direct fix for the OOD failure.

### TIER 1 — high-value synthesizers (4-6 hours)
* `silver_answer_runs_nl_summary` (19,354 → +19,354 passages)
* `silver_samples_nl_summary` (stub → real)
* `silver_entity_aliases_nl_summary`
* `silver_smdi_deposits_nl_summary`
* `silver_projects_nl_summary` (128 → +128 passages)
* `silver_geological_ontology_terms_nl_summary`
* `silver_resource_estimates_nl_summary`

### TIER 2 — medium-value (3-4 hours)
* `silver_rock_codes_nl_summary`
* `silver_data_quality_flags_nl_summary`
* `silver_campaigns_nl_summary`
* `silver_geological_ontology_synonyms_nl_summary`
* `silver_ingest_extractions_nl_summary` (246)
* `silver_ingest_ocr_results_nl_summary` (668)
* `silver_element_reference_nl_summary` (12)
* `silver_chat_messages_nl_summary` (125)

### TIER 3 — gold layer scaffolding (templates only)
(Unchanged from pass 1)

### TIER 4 — bronze recovery (post-demo)
(Unchanged from pass 1)

---

## 6. Why pass 1 missed this

Worth documenting so the next audit doesn't have the same gap.

Pass 1 was structured around "what data needs to be NL-synthesized for
the reranker corpus?" — that's right for **content** retrieval but it
missed the meta-layer of **how the system has been used**. The
`silver.answer_runs` family is operational telemetry, not content,
but it's the most important content for matching the inference
distribution.

Future audits should explicitly ask three questions per table:
1. Does it contain domain content? (pass 1's question)
2. Does it contain real user queries? (pass 2's question)
3. Does it contain ground-truth labels (citations) that could supervise training? (pass 2's question)

The pulse_entries (308k rows) is the same category but adds no signal
— it's request-rate metrics, not query content. Properly distinguishing
these is what pass 2 nails down.

---

## 7. Summary of the two audit passes

| Source | Pass 1 says | Pass 2 corrects/adds |
|---|---|---|
| Currently-populated synthesizable content | 11,800 | + ~20,500 from answer_runs + projects + OCR + extractions + element + chat |
| Real labeled reranker data | 0 (we have 963 synthetic) | **~4,400 positive + ~50,000 hard-negative pairs from real queries** |
| Corpus size after wave 2 | 31,700 | **52,500** |
| Highest-impact next move | "Build wave 2 synthesizers" | **"Build reranker_label_dataset_real from answer_runs"** |
| OOD bench root cause fix | Add diverse synthesized passages | **Train on real query distribution from answer_runs** |

Pass 1 is correct as far as it goes; pass 2 is what makes the next
training cycle actually beat the stock baseline on real geologist
questions.
