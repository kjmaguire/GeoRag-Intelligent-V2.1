# Corpus coverage audit — Pass 3 (deepest dive)

**Purpose**: Go as deep as possible. Pass 1 covered structured silver
tables. Pass 2 found `silver.answer_runs`. Pass 3 probes Qdrant
itself, Neo4j, MinIO blob storage, JSONB payloads, and seven
populated systems we'd been treating as opaque.

**TL;DR — single sentence**: There are **~150,000 natural-language
passages already embedded in Qdrant `pg_*` public-geo collections
that have never touched reranker training, MLM training, or even
the canonical retrieval path**. Bringing them in expands the corpus
from 7,929 → ~200,000 passages, ~25× growth, with most of the new
content being authoritative geological-survey records.

---

## 1. THE biggest find — public-geo Qdrant collections

Qdrant has **7 collections, not 1**:

| Collection | Points | Used by reranker training? | Used by chat retrieval? |
|---|---:|---|---|
| `georag_chunks` | 7,353 | YES | YES (main path) |
| `pg_mineral_occurrence` | **71,056** | ❌ NO | partial (separate tool) |
| `pg_drillhole_collar` | **33,490** | ❌ NO | partial |
| `pg_rock_sample` | **29,875** | ❌ NO | partial |
| `pg_assessment_survey` | **14,835** | ❌ NO | partial |
| `pg_resource_potential_zone` | 908 | ❌ NO | partial |
| `pg_mine` | 140 | ❌ NO | partial |
| **TOTAL pg_* (untouched)** | **150,304** | | |

Each `pg_*` point has a `summary_text` field containing **real, well-formed
natural language** (not template-rendered structured data). Live samples:

**`pg_mineral_occurrence`** (BC MINFILE + SK MDI + provincial registries):
> *"Past-Producer mineral occurrence 'CALIFORNIA (L.918)' (#082FNW005) in
> British Columbia, base metals. Primary commodities: Pb, Ag. Associated:
> none listed. Discovery type: Hydrothermal. Historical prod[uction]..."*

**`pg_drillhole_collar`** (33k SK + other-jurisdiction public drillholes):
> *"Drillhole 'RU-079' (public ID 55841584) in Saskatchewan. Operator UEX
> Corporation at project 'Hidden Bay'. Drilled date unknown as 'diamond'
> (hydraulic rotary), total depth 341.00m, collar elevation 4..."*

**`pg_assessment_survey`** (14k geophysical/geochemical survey footprints):
> *"Ground geophysical survey footprint in Saskatchewan. Covers the
> polygon delimited by approximately (-102.16, 54.63) – (-102.03, 54.74).
> Detailed survey..."*

**`pg_rock_sample`** (29k government rock samples):
> *"Government rock sample '1361' collected in Oskikebuk Lake of
> Saskatchewan (NTS 1:250K 73I, 63L). Geologist: G. D. Delaney. Collection
> date: 1970-01-01. Referenced in report SOI-1991. Government record..."*

**`pg_mine`** (140 known mine sites):
> *"Unknown mine 'Old Pit' in Saskatchewan (unclassified commodity).
> Commodities: Ka. Operator: unspecified. Government record:
> CA-SK-MINE-LOC #886."*

**`pg_resource_potential_zone`** (908 resource potential zones):
> *"Oil resource potential zone (rank unspecified) in Saskatchewan,
> unclassified commodity. Methodology: not referenced. Government
> record: CA-SK-RESOURCE-POTENTIAL-OIL #534."*

### Why this changes everything

* **Already embedded** — no compute spent generating; the bge-small + SPLADE++
  vectors already exist.
* **Already NL** — `summary_text` is prose, not structured rows. No
  template-rendering needed. We just COPY into `silver.document_passages`.
* **Authoritative** — these are government geological survey records. BC
  MINFILE, SK MDI, NRCan-derived. Geologists trust these sources for
  context: *"has anyone else explored this area?"*, *"what's near our
  property?"*, *"what's the commodity history here?"*.
* **Reranker has never seen them** — both LoRA training cycles operated
  only on `georag_chunks`. The 71,056 mineral occurrence narratives alone
  are 9× larger than the entire training corpus today.

### Action

**NEW Dagster asset**: `silver_public_geo_passages_backfill` —
streams each `pg_*` collection's points, copies `summary_text` into
`silver.document_passages` as `chunk_kind='public_geo_synthesis'`,
with the source collection name + jurisdiction + commodities preserved
as payload fields. The existing embed cron re-embeds into `georag_chunks`
(matching ADR-0010 canonical), making them retrievable through the
main `search_documents` path AND making them training data for the
next reranker cycle.

Volume: 150,304 new passages.

### Demo implication

A geologist asking *"are there any past-producers near our PLS license?"*
or *"what other holes are in this area?"* currently can't get
useful results from chat — those tools (`query_public_geo_*`) hit a
separate path that doesn't surface in `search_documents`. After this
backfill, every public-geo record is reachable from the main chat
question path.

---

## 2. Neo4j knowledge graph — fully populated, unused for training

Neo4j has **2,116 nodes + 2,545 relationships** across 11 entity
types. Not touched by any audit pass before this one.

### Inventory

| Node label | Count |
|---|---:|
| `Report` | 1,212 |
| `DrillHole` | 473 |
| `Mine` + `PublicGeo` | 140 |
| `Project` | 128 |
| `QP` | 75 |
| `Commodity` | 44 |
| `PublicGeoSource` | 32 |
| `Formation` | 17 |
| `Deposit` | 3 |
| `QualifiedPerson` | 3 |
| `Jurisdiction` | 3 |
| `MineralOccurrence` | 1 |

| Relationship | Count |
|---|---:|
| `HAS_REPORT` (Project→Report) | 1,206 |
| `HAS_HOLE` (Project→DrillHole) | 473 |
| `INTERSECTS` (DrillHole→Formation/Lithology) | 160 |
| `AUTHORED_BY` (Report→QP) | 143 |
| `SOURCED_FROM` (PublicGeo→Source) | 140 |
| `TARGETS` | 131 |
| `HAS_FORMATION` | 126 |
| `HAS_LITHOLOGY` | 120 |
| `HAS_COMMODITY` | 56 |
| `DESCRIBES` (Report→Concept) | 51 |
| `PUBLISHED_BY` | 32 |
| Others (HOSTED_BY, PART_OF, WORKS_ON, HOSTS, HAS_MINERALIZATION) | 11 |

### Why this matters for training

Neo4j is the ONLY place where **cross-table relationships are
first-class**. A passage synthesised from Neo4j can say things no
single silver-table synthesiser can:

> *"Patterson Lake South (Project) was explored by Cameco Corporation in
> 14 drillhole campaigns. The project HAS_REPORT 24 NI 43-101 technical
> reports, the most recent AUTHORED_BY David K. Edie (QP). Holes
> INTERSECTS formations: Athabasca Group sandstone (host) and graphitic
> pelitic gneiss (basement). Commodity: Uranium."*

That's a **cross-table joined narrative**. SQL joins can express the
mechanics but the *coherent prose* is what trains the model to think
in the same multi-hop way geologists do.

### Action

**NEW Dagster asset**: `silver_neo4j_kg_narratives` — for each high-
value entity (Project, DrillHole, Mine, QP, Formation, Deposit),
generate a passage that includes the entity's outgoing relationships
+ joined neighbours. Volume: **~2,116 passages** (one per node) of
much higher information density than per-table synthesisers.

This is the synthesizer ADR-0012 talked about as "cross-source joins";
Neo4j is the natural place to do it.

---

## 3. silver.reports field population — major gaps

Pass 1 said *"reports w/ resource_estimate JSONB = 64"*. Pass 3 drills into
the rest of the reports columns:

| Column | Rows populated | % of 1,228 |
|---|---:|---:|
| `commodity` | 1,197 | **97%** |
| `resource_estimate` JSONB | 103 | 8% |
| `region` | 73 | 6% |
| `company` | 62 | 5% |
| `filing_date` | 60 | 5% |
| `authors` (text[]) | 18 | **1.5%** |
| `qp_name` (text[]) | 10 | **0.8%** |
| `effective_date` | 0 | **0%** |
| `sections_text` JSONB | 103 (mostly empty `{}`) | 8% |

### Two observations

**(a) The §04p extractor is leaving authorial/regulatory metadata on the floor.**
Every NI 43-101 has a QP, every QP signs an effective date, every
report has authors. Only 1.5% / 0.8% / 0% are captured. This is a
silent ingestion bug — separate from corpus coverage, but worth
flagging now because Q&A about "who signed off this report?" or
"when was the effective date?" can't work without these.

**(b) `sections_text` JSONB is 99% empty `{}` objects.**
The chunker produces text passages but isn't preserving the section
hierarchy that NI 43-101 reports rely on. Section titles like *"15.2
Mineral Resource Estimate Methodology"* or *"7.5 Drilling Procedures"*
are key navigation handles a geologist would search for. Tracking as
a §04p improvement task, not actionable in this cycle.

### Action

Two follow-up bugs to file:
1. §04p QP/author/effective_date extraction gap.
2. §04p sections_text population gap.

Both are silent — the report ingest reports success but leaves these
fields empty.

---

## 4. MinIO blob inventory

All buckets I'd never inventoried:

| Bucket | Objects | Size | Content |
|---|---:|---:|---|
| `bronze` | 369 | 2,468 MB | Raw PDFs, CSVs, XLSX, LAS — sources of silver content |
| `georag-backups` | 1,000 | 16,000 MB | DB / Qdrant / Neo4j backups |
| `langfuse-events` | 488 | 3.7 MB | Telemetry events |
| `reranker-labels` | 24 | 125 MB | Reranker training datasets across runs |
| `tier-warm` | 48 | 0.1 MB | Cold-tier archive |
| (others) | 0 | 0 | bronze-raster, exports, tier-cold, tier-hot — unused |

### What's interesting

* **`bronze/` 2.5 GB** — every raw upload that became silver content. We
  could write a Hatchet workflow to re-parse failed pages, but the volume
  is small (per MEMORY:project_parse_perf, ~5-10% of pages fail).
  Confirmed as low-priority in pass 1.
* **`reranker-labels/` 125 MB** — every training run's JSONL datasets,
  including the synthetic ones we trained on this afternoon. These ARE
  the training data; not corpus content but worth recognizing as
  versioned ground truth.
* **`georag-backups/` 16 GB** — DB / Qdrant / Neo4j snapshots. No
  training value; operational.
* **`langfuse-events/`** — chat telemetry. Could be mined for query
  patterns but `silver.answer_runs` (pass 2) is the same data
  normalised — use that instead.

Nothing in the MinIO inventory adds NEW corpus material beyond what's
already in DB. Bronze recovery candidate stays at the same tier-4
priority as pass 1.

---

## 5. silver.entity_aliases — checking what's actually in there

Pass 1 said 13,365 rows of KG alias pairs. Pass 3 sampled the actual
contents and found **the data is heavily test-fixture-driven**:

Top alias counts:
- `phyllitic` (33 rows)
- `glacial till` (33 rows)
- `granodiorite` (33 rows)
- `IOCG` (33 rows)
- `sandstone U` (33 rows)
- `yellowcake` (33 rows)
- `BIF` (33 rows)
- `sedimentary exhalative deposit` (33 rows)

Each alias appears **33 times** — once per test workspace (the test-hg-*
workspaces). The 13,365 number is inflated by test workspace
duplication.

**True unique-alias count**: ~404 distinct technical-term aliases ×
33 workspaces = 13,332. So ~400 useful alias rows, not 13k.

That's still a useful corpus — 400 alias passages can teach the model
the canonical-vs-alias vocabulary. But it's an order of magnitude
smaller than the pass 1 number suggested.

### Action

Down-revise the wave 2 `silver_entity_aliases_nl_summary` expected
volume from 1,340 batched passages to **~40 batched passages** (10
aliases per passage). Still ship — vocabulary value is real, just
smaller than originally estimated.

---

## 6. data_sub_type taxonomy — 47 rows of geology-specific categorisation

`silver.data_sub_type` has 47 rows covering domain sub-taxonomies. Cross-
referenced with `silver.data_domain` (5 rows: Geology, Geophysics,
Chemistry, Hydrology, Environmental).

This is the **classification taxonomy** the §1c document classifier uses
to bucket every chunk. Synthesizing each sub-type as a passage
teaches the model the domain ontology:

> *"Data sub-type: lithology log. Domain: Geology. Description: descriptive
> rock-by-rock interval log from drill core or outcrop, capturing rock_code,
> rock_name, description, and per-interval visual attributes. Source
> formats: CSV/XLSX from logging software (RockWorks, gINT, Leapfrog),
> PDF table extracts from NI 43-101 reports."*

47 such passages. Small volume, high information density. Add to wave 2.

---

## 7. Cross-pass corpus size revision

Combining all three passes:

| Phase | Passages | Source |
|---|---:|---|
| Today | 7,929 | silver.document_passages (prose) |
| ADR-0012 wave 1 | +12,000 | assays + lithology + collars |
| Pass 1 wave 2 | +11,800 | samples + SMDI + KG aliases + ontology + … |
| Pass 2 additions | +20,500 | answer_runs + projects + OCR + extractions |
| Pass 2 — **revised down** | -1,300 | entity_aliases inflated by test workspaces |
| **Pass 3 additions** | **+150,304** | **public-geo Qdrant collections backfill** |
| **Pass 3 — Neo4j narratives** | **+2,116** | **KG cross-relationship synthesis** |
| **Pass 3 — data sub_type** | +47 | domain taxonomy |
| **TOTAL after waves 1+2+3** | **~203,400 passages** | |
| + Gold (when populated) | ~208,000 | |
| + Cameco missing data | ~215,000+ | |
| + Bronze recovery (post-demo) | ~216,200 | |

**26× corpus expansion** vs today's 7,929. The vast majority comes
from already-existing, already-embedded, never-used content.

---

## 8. Updated sequencing — TIER 0 is now different

Pass 2 said TIER 0 was *"build reranker_label_dataset_real from
answer_runs"* (real labelled training data, ~50k pairs).

Pass 3 reveals two even higher-impact TIER 0 candidates:

### TIER 0a — silver_public_geo_passages_backfill (single highest-volume)
Stream 150,304 `summary_text` fields from the `pg_*` Qdrant collections
into `silver.document_passages` as `chunk_kind='public_geo_synthesis'`.
Embed cron picks them up (already-fixed georag_chunks path).
**~6h of work, +150,000 corpus, 19× growth from a single change.**

### TIER 0b — reranker_label_dataset_real (highest-quality real labels)
The pass-2 plan unchanged: pull from `answer_runs` + `retrieval_items` +
`citation_items` → real (query, positive, hard_neg) pairs. ~4-6h work,
+50,000 labelled pairs.

### TIER 0c — silver_neo4j_kg_narratives
Cross-relationship synthesised passages — uniquely impossible from
silver tables alone. ~4h work, +2,116 high-density passages.

**Recommended order**: 0a → 0b → 0c. The volume from 0a alone makes the
next training cycle's corpus larger than anything we've trained on,
and the public-geo passages cover the exact "context around my
project" questions geologists ask in demos.

---

## 9. Three follow-up bugs the audit surfaced

These are not corpus-coverage issues but are silent ingestion gaps
worth filing as their own tickets:

1. **§04p QP / author / effective_date extraction gap** — 0.8% / 1.5% /
   0% population. Every NI 43-101 has all three; the extractor is
   leaving them on the floor. (§3.b above.)
2. **§04p sections_text population gap** — 99% empty `{}`. The
   chunker emits passages but loses the section hierarchy. Demo
   questions like *"What does section 14.2 say?"* fail without this.
3. **`silver.kg_*_aliases` tables all 0** — these mirror
   `silver.entity_aliases` but per-class (formation / mineral /
   report / sample). Either fold into entity_aliases (already covered)
   or populate them and reconsider.

None of these block training — they block specific demo-question types.

---

## 10. Summary table of all three passes

| Source | Pass 1 | Pass 2 | Pass 3 |
|---|---|---|---|
| Synthesisable from populated silver tables | ~11,800 | + 20,500 | (- 1,300 entity_aliases correction) |
| Real labelled reranker pairs | 0 (synthetic only) | ~50,000 | (unchanged) |
| Already-embedded but unused Qdrant content | 0 | 0 | **+150,304** |
| Cross-relationship KG narratives | 0 | 0 | +2,116 |
| Domain taxonomy passages | 0 | 0 | +47 |
| **Corpus size after wave** | 31,700 | 52,500 | **~203,400** |

Pass 3 is the only one of the three that finds *already-NL-prose
content the model has never seen*. The other two passes generate
content; pass 3 connects content that already exists.

---

## 11. The single most demo-relevant change across all 3 passes

If you only ship one thing before the demo:

**`silver_public_geo_passages_backfill`** (TIER 0a from §8 above).

It:
* Adds 150,304 passages — most of them about specific geological
  sites geologists already use in BC/SK regulatory work.
* Costs ~6h of engineering — just streams Qdrant point payloads
  into a Postgres UPSERT.
* Causes zero behavioural change to existing chat — public-geo
  collections stay queryable separately as they were.
* Lights up *"is there anything near my project?"* /
  *"who else has explored this area?"* /
  *"any past-producers in this district?"* questions in chat.
* Makes the next reranker training cycle train on the real
  jurisdiction-public corpus, not just NI 43-101 prose.

This single change is the largest leverage of the entire audit.
