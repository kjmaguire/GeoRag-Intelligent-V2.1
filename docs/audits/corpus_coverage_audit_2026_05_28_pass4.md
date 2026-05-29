# Corpus coverage audit — Pass 4 (codebase + raw source + history)

**Purpose**: Continue going deeper. Passes 1-3 covered DB tables, Qdrant
collections, and Neo4j. Pass 4 probes the **codebase itself** (docs,
migrations, workflows, agent prompts), the **raw bronze source data**
that the Qdrant summaries were rendered FROM, and **historical training
artifacts** sitting in MinIO unused.

**TL;DR — single sentence**: We have ~66,000 lines of domain-rich
markdown documentation, ~1.5 GB of raw provincial geological-survey
records as GeoJSON in bronze, and a historical 75 MB training dataset
in MinIO that the model has never trained on — all sitting as
training-corpus dark matter on the same machine.

---

## 1. The codebase is itself a geological-domain corpus

| Source | Volume | What it contains |
|---|---:|---|
| `docs/**/*.md` | **66,886 lines / 405 files** | ADRs, architecture appendices, design specs, data dictionary, KG schema, runbooks, audits |
| `database/migrations/*.php` | **188 files** | Per-migration docblocks explaining each schema decision + geological intent |
| `src/fastapi/app/hatchet_workflows/*.py` | **47 files** | Workflow definitions with step-level descriptions of geological ingestion pipelines |
| `src/fastapi/app/agent/prompts/*.py` | **10 files** | System prompts with embedded geology examples, refusal patterns, citation patterns |
| `resources/js/Pages/*.tsx` | **96 files** | Frontend page text — user-facing geology explanations |

### What's actually in the high-value docs

* **`docs/architecture/appendix/F-data-dictionary.md`** — describes a
  planned auto-generated data dictionary. The generator (Dagster asset
  `data_dictionary_dump`) WOULD emit `docs/architecture/data_dict/<schema>.md`
  per schema with per-table contract templates. Currently `data_dict/`
  is empty. **Building the generator would produce ~150-200 per-table
  markdown docs of pure domain-context prose.**

* **`docs/architecture/appendix/H-knowledge-graph-schema.md`** — master
  Neo4j ontology document. Defines every node label (`:Project`,
  `:DrillHole`, `:Formation`, `:RockUnit`, `:MineralOccurrence`,
  `:Citation`, `:Document`, ...), every required property, every
  upsert/conflict rule with example Cypher. This is a domain-defining
  document — every concept in our system is here.

* **`docs/architecture/appendix/M-agents-and-ml-catalog.md`** — catalog
  of every agent + ML model in the system with capability descriptions.

* **`docs/architecture/appendix/N-agentic-and-retrieval-catalog.md`** —
  catalog of retrieval strategies, intent classifications.

* **12 ADRs** — each explains WHY a domain decision was made. Why
  bge-reranker over Cohere? Why parent-child chunking? Why this Neo4j
  label canonicalisation? These are training-grade explanations.

* **Design specs**: `parent_child_chunker_spec.md`,
  `context_prep_spec.md`, `multi_turn_resolution_spec.md`,
  `data_quality_flags_design.md` — geology-domain reasoning behind
  each subsystem.

### Why this matters for training

The model needs to know its own **conceptual ontology**, not just
literal terms. The docs/ corpus contains:

* Definitions of every entity type the system handles.
* Examples of how those entities relate.
* Operational vocabulary (workspace, project, hole, sample, assay,
  composite, intersection, QP, citation, refusal).
* The ACTUAL language we use internally to describe geology.

Synthesizing the docs/ corpus into training-grade passages teaches the
model to **speak in the GeoRAG-system's vocabulary**, which is what
chat queries will use.

### Action

* **Asset**: `corpus_docs_markdown_passages` — chunks each markdown
  file at heading boundaries, ships passages with
  `chunk_kind='internal_docs'`. Volume: ~3,000-5,000 passages.
* **Build the planned `data_dictionary_dump` generator** — when it
  runs, emits ~150-200 per-table markdown files that become another
  ~1,000 passages.
* **Agent prompt mining** — extract the embedded geology examples (e.g.
  `example_system.py`, `oiur_section.py`) as standalone passages.
  Volume: ~50-100 high-density example passages.

Combined: **~4,000-6,000 passages** from the codebase itself.

---

## 2. Historical training datasets sitting unused in MinIO

`reranker-labels/` bucket has **24 objects across 6 historical runs**.
The largest:

```
v1/run_id=28d9013e-d1be-4dee-bf40-f0bc4198a503/
  train.jsonl                    74,849 KB  (~75 MB)
  test.jsonl                     46,994 KB
  val.jsonl                       2,758 KB
  generated_queries.parquet       1,914 KB
```

For comparison, the dataset we trained on this afternoon
(`181fdf15...`) was 724 KB train.jsonl. The `28d9013e` run is
**~100× larger**, with ~75 MB of (query, positive, hard_negs) tuples
that we never trained on.

### What this is

Each historical training run is a snapshot of:
* Qwen3-generated synthetic queries over a previous corpus state
* Mined Qdrant hard negatives
* Critique-filtered relevance scores

The `generated_queries.parquet` files are particularly valuable —
they're **Qwen3's geological-query phrasings**, with metadata about
which chunk they target. The 28d9013e run alone has ~50,000-100,000
synthetic queries, each tied to a specific source chunk.

### Why this matters

Two uses for this historical data:

(a) **Pre-training corpus boost** — the generated_queries are
geological prose that the model has never seen as training input.
Feeding them into MLM training adds ~100k+ query-shaped passages.

(b) **Reranker training augmentation** — the 75 MB train.jsonl is a
ready-made reranker labelled dataset. Could mix with the
pass-2-proposed `reranker_label_dataset_real` for more training
volume.

### Action

* **Asset**: `historical_reranker_datasets_recovery` — reads the
  6 historical `reranker-labels/v1/run_id=*/` prefixes, deduplicates
  queries across runs (drops queries already present in the current
  synthetic dataset), and emits an additional JSONL split.
* **Volume**: estimated ~100,000 unique queries + their positive
  chunk pairs across the 6 historical runs.

---

## 3. Raw bronze public-geoscience source data (1.5 GB unprocessed)

Pass 3 found the `pg_*` Qdrant collections (150,304 points) and noted
they were rendered from "BC MINFILE + SK MDI + provincial registries".
Pass 4 located the **raw source**:

```
bronze/public_geoscience/
├── CA-BC/CA-BC-MINFILE/        — 8 GeoJSON files, ~36 MB each = ~144 MB
└── CA-SK/                       — 224 objects, ~1.4 GB
    ├── CA-SK-ASSESSMENT-AIRBORNE/  — 61 MB GeoJSON files (airborne surveys)
    ├── CA-SK-MINFILE/              — (paste 3 confirmed pg_mineral_occurrence)
    ├── CA-SK-MINE-LOC/             — (mine locations)
    ├── CA-SK-DRILLHOLE/            — (drillhole collars)
    ├── CA-SK-ROCK-SAMPLE/          — (rock samples)
    ├── CA-SK-RESOURCE-POTENTIAL-*/ — (resource potential zones)
    └── CA-SK-ASSESSMENT-*/         — (assessment surveys)
```

These are the **GeoJSON FeatureCollections** that the ingest pipeline
read and rendered into Qdrant `summary_text` payloads. Each Feature
has **far more attributes** than the rendered summary captures:

* `properties.commodities` — array, often with sub-array of pathfinder
  elements
* `properties.discovery_type` — Hydrothermal, Sedimentary, Magmatic, etc.
* `properties.host_rock` — granitic, metasedimentary, etc.
* `properties.production_history` — past tonnage, grade, production years
* `properties.deposit_age` — geological age of mineralisation
* `properties.exploration_history` — survey reports, drill campaigns
* `properties.references` — external publication citations
* `properties.geometry` — exact spatial polygon / point coordinates

The rendered `summary_text` (256 chars avg) captures maybe 10-20% of
this. **The raw GeoJSON is a richer corpus source** than the Qdrant
summaries.

### Two paths forward

(a) **Quick path**: Backfill pg_* summaries into silver.document_passages
(pass 3 TIER 0a — already proposed). Gets the existing rendered prose
into training fast.

(b) **Richer path**: Write a per-feature-class GeoJSON synthesizer
that produces 500-1000-char passages with the FULL property set
(not just the rendered subset). Volume same (~150k passages) but
each one ~3-5× more information-dense.

Recommend (a) ship first as cheap win, (b) ship in a follow-up cycle
once we measure the bench impact of (a).

---

## 4. The actual report corpus is uranium-heavy

Sample of recent populated `silver.reports`:

```
title                       | company             | commodity | region
NI 43-101 Technical Report  | Fission Uranium Corp | uranium  | Athabasca Basin
NI 43-101 Technical Report  | Fission Uranium Corp | uranium  | Athabasca Basin
[repeated 14× for 2024 filings]
Rupert Resources Ltd.       | Rupert Resources Ltd | (null)   | Canada (2025)
```

Combined with the Cameco PLS data referenced throughout, **the
training corpus is dominated by uranium + Athabasca Basin**.

### Implications for demo

* The model will be **strong** on uranium vocabulary (U3O8, unconformity,
  Athabasca Group, McArthur River, Cigar Lake, sandstone-hosted,
  pitchblende, uraninite, basement-hosted, supergene/hypogene
  classifications).
* The model will be **weak** on other commodities (Au, Cu, Pb-Zn, Ni,
  PGE) and other tectonic settings (IOCG, porphyry, SEDEX, VMS,
  orogenic gold).
* The Earle textbook (added today) partially mitigates this for
  general geological vocabulary — but it doesn't cover specific deposit
  styles in depth.

### Action — corpus diversification

Two routes to broaden the commodity / tectonic coverage:

(a) **Ingest more public NI 43-101 reports** — SEDAR has thousands
covering every commodity. A 50-report sweep of Au + Cu + Ni
documents would balance the corpus. ~1-2 day Hatchet workflow.

(b) **Pull from `pg_mineral_occurrence` heavily** — the BC MINFILE +
SK MDI Qdrant collections cover all commodities. If we backfill those
into the training corpus (pass-3 TIER 0a), the model gets exposure to
~70k occurrences across all commodities and deposit types.

(b) is the cheaper win. Stack rank: (b) first, (a) later.

---

## 5. The skewed real-distribution observation

`silver.answer_runs` (pass 2 — 19,354 real queries) was filed within
the same uranium/Athabasca-heavy workspace as the corpus. So both
the training distribution AND the inference distribution are
uranium-heavy.

This is actually **good news for the demo** — both sides match.
The reranker should learn what's commonly asked.

But **be aware**: if the demo audience asks anything off the uranium
beam (e.g. "explain the Carlin Trend gold model"), the model will
likely refuse or hedge. Wire that into the demo script accordingly.

---

## 6. Database migrations as domain narratives

188 PHP migration files in `database/migrations/`. Sampling shows each
has a leading docblock explaining the schema decision in plain English:

> *"Plan §6c — OER textbook ingest preparation. Adds per-row license /
> attribution columns to silver.reports so any external content (open
> educational resources, public-domain government publications,
> third-party reference texts) can carry its own provenance independent
> of the workspace's NI 43-101 corpus. ..."*

(That's from this morning's `2026_05_28_180000_add_license_attribution_to_silver_reports.php`.)

These are **historical context narratives** — each one captures the
geological reasoning behind a system change. ~30-100 lines of prose per
migration × 188 migrations = **~10,000 lines of system-history
narrative**.

### Action

Lower priority than the corpus expansions in §1-3, but a stretch
asset: `corpus_migration_docblocks` — extracts the docblock from each
migration, emits one passage per migration. Volume: ~188 passages.

---

## 7. Updated cross-pass corpus projection

Combining all four passes:

| Phase | Passages | Source |
|---|---:|---|
| Today | 7,929 | silver.document_passages (prose) |
| ADR-0012 wave 1 | +12,000 | (committed) |
| Pass 1 wave 2 | +11,800 | structured silver tables |
| Pass 2 additions | +20,500 | answer_runs / projects / OCR |
| Pass 2 — entity_aliases correction | -1,300 | test-workspace duplication |
| Pass 3 — public-geo backfill | **+150,304** | Qdrant pg_* summary_text |
| Pass 3 — Neo4j narratives | +2,116 | KG cross-relationships |
| Pass 3 — data sub_type | +47 | taxonomy |
| **Pass 4 — docs/ markdown** | **+4,000** | architecture + ADRs + design specs |
| **Pass 4 — data dictionary generator** | +1,000 | when run, emits per-table mds |
| **Pass 4 — agent prompt examples** | +75 | high-density domain examples |
| **Pass 4 — historical reranker queries** | +100,000 | Qwen3 syntheses across 6 historical runs |
| **Pass 4 — migration docblocks** | +188 | system-history narratives |
| **Pass 4 — raw GeoJSON synthesis (option (b))** | (replaces P3) | full-property GeoJSON re-render |
| **TOTAL after all 4 waves** | **~308,500 passages** | |
| + Gold (when populated) | ~313,000 | |
| + Cameco missing data | ~320,000+ | |
| + Bronze recovery | ~321,200 | |

**~39× corpus expansion** vs today's 7,929. Most of the gain from
already-existing content the system has never trained on.

---

## 8. Updated TIER 0 ordering (incorporating pass 4)

Pass 3 ordered TIER 0 as: (a) public-geo backfill → (b) real reranker
data → (c) Neo4j narratives.

Pass 4 inserts two between them:

* **TIER 0a-new**: `historical_reranker_datasets_recovery` — pull the
  75 MB `28d9013e` train.jsonl + its 5 sibling runs as additional
  training pairs. **Fastest, single-day, no new code.**

* **TIER 0b-new**: `silver_public_geo_passages_backfill` (was pass-3 0a)

* **TIER 0c-new**: `reranker_label_dataset_real` (was pass-2 0a)

* **TIER 0d-new**: `corpus_docs_markdown_passages` — chunks docs/ into
  passages. Single afternoon of work.

* **TIER 0e-new**: `silver_neo4j_kg_narratives` (was pass-3 0c)

The historical-recovery is now top priority because it's literally
"unzip these JSONL files we already paid for, mix into the next
training run". Zero engineering — operational only.

---

## 9. Pass-4 specific demo-ready question patterns this unlocks

| Real query type | Coverage today | After pass 4 |
|---|---|---|
| "How does the chunker work?" | ❌ | ✓ via docs/ corpus |
| "What's the difference between U3O8 and yellowcake?" | ✓ (textbook) | ✓✓ (docs + KG aliases) |
| "Show me the data dictionary for silver.assays_v2" | ❌ | ✓ when generator runs |
| "What other deposits are like Cigar Lake?" | ❌ | ✓ via Neo4j KG + similar pg_* points |
| "Who else has explored CA-BC for Cu-Mo porphyry?" | ❌ | ✓ via pg_mineral_occurrence backfill |
| "Explain the system's ontology to me" | ❌ | ✓ via H-knowledge-graph-schema.md passage |

Two of these (the Cigar Lake analogue + the porphyry exploration
history) are exactly the kind of *open-ended, comparative,
cross-deposit* questions a senior geologist or exploration manager
asks — and they're exactly the questions today's narrow
prose-only corpus can't answer.

---

## 10. Pass 4 in one paragraph

Pass 4 finds that the **same machine** running this conversation
already holds: **66,886 lines of geological-domain markdown
documentation**, **1.5 GB of raw provincial-survey GeoJSON in bronze
that's only 10-20% rendered into the pg_* Qdrant summaries**, a
**75 MB historical reranker training dataset** sitting in
`reranker-labels/v1/run_id=28d9013e.../` from a prior cycle that
never made it into a fine-tune, **47 Hatchet workflow definitions**
documenting every ingestion pipeline, and **188 schema-migration
docblocks** narrating the geological reasoning behind every
table-shape decision. Stacked together they make today's 7,929-passage
corpus look like a sample, not a corpus. **~308,500 passages** is
reachable across waves 1-4. None of this requires new ingestion or
new GPU work — it's pure plumbing to bring already-existing,
already-paid-for content into `silver.document_passages` where
training can see it.
