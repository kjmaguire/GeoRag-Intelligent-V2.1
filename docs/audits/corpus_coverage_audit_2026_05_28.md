# Corpus coverage audit — 2026-05-28

**Purpose**: identify exactly what's in (and missing from) the bronze/silver/gold
layers, so the next reranker training run captures everything a geologist
would ask about in a demo.

**TL;DR**

* Today's corpus (`silver.document_passages`): 7,929 prose chunks from NI 43-101 reports + Earle textbook.
* ADR-0012 wave 1 adds ~12,000 structured passages from assays / lithology / collars.
* This audit found **~12,000 MORE structured passages we can synthesize today** from already-populated silver tables (samples, SMDI public-geo, KG aliases, CGI ontology, QA flags, campaigns).
* Gold layer is currently empty in this env (0 rows everywhere) — schemas exist, templates can be pre-built so they flow the moment those views materialise.
* **Five domain-critical silver tables are EMPTY** — alteration, mineralization, geochemistry, structure (4 rows), geophysics_surveys (1 row). For demo realism, these need actual data ingestion before the next training cycle, not template work.

---

## 1. Inventory of what's actually populated

Row counts as of 2026-05-28 22:30 UTC on the dev environment.

### Bronze
| Table | Rows | Value for training |
|---|---:|---|
| `bronze.ingest_manifest` | 39,744 | Low — file-level metadata (SHA, MIME, size). Not domain content. |
| `bronze.provenance` | 2,741 | Low — audit ledger. |
| `bronze.manifest` | 10 | Negligible. |

Bronze is mostly raw blobs (PDFs / CSVs / XLSX / LAS) in MinIO `bronze/...` keys. Text inside those files lands in silver as parsed content. **There is no significant textual content in bronze TABLES that isn't already in silver.**

### Silver — populated
| Table | Rows | Status in ADR-0012 wave 1 | Demo value |
|---|---:|---|---|
| `silver.entity_aliases` | **13,365** | NOT scaffolded | HUGE — KG alias pairs ("X is also known as Y") |
| `silver.lithology` | **11,298** | ✓ implemented | HUGE — interval-by-interval rock identifications |
| `silver.lithology_logs` | 11,302 | not addressed | Likely duplicate of silver.lithology — confirm |
| `silver.document_passages` | 7,929 | (the existing corpus) | — |
| `public.smdi_deposits` | **6,012** | NOT scaffolded | HUGE — Saskatchewan public-geo deposit index |
| `silver.samples` | **4,379** | TODO stub | HUGE — physical sample metadata |
| `silver.data_quality_flags` | 2,215 | NOT scaffolded | MEDIUM — QA findings as prose |
| `silver.reports` | 1,228 | (already chunked) | — |
| `silver.collars` | 567 | ✓ implemented | (covered) |
| `silver.drill_traces` | 567 | NOT scaffolded | LOW — 3D survey points, hard to narrate |
| `silver.assays_v2` | 540 | ✓ implemented | (covered) |
| `silver.geological_ontology_synonyms` | 134 | NOT scaffolded | HIGH — synonym pairs feed vocab |
| `silver.geological_ontology_terms` | **83** | NOT scaffolded | HIGH — 47 commodities + 29 ages + 7 resource classes WITH definitions |
| `silver.reports` w/ `resource_estimate` JSONB | 64 | NOT scaffolded | HIGH — resource statement prose |
| `silver.rock_codes` | 30 | NOT scaffolded | MEDIUM — rock taxonomy definitions |
| `silver.campaigns` | 15 | NOT scaffolded | MEDIUM — drilling campaign rollups |
| `silver.structure` | 4 | TODO stub | (empty — see §3) |
| `silver.geophysics_surveys` | 1 | TODO stub | (empty — see §3) |

### Silver — empty (real data needed)
| Table | Status | What questions this blocks |
|---|---|---|
| `silver.alteration` | 0 | "What's the dominant alteration in this zone?" / "Is it chloritic / sericitic / argillic?" |
| `silver.mineralization` | 0 | "Is mineralisation disseminated, vein-hosted, or massive?" / "Style of mineralisation?" |
| `silver.geochemistry` | 0 | "What are the pathfinder elements?" / "Rock geochemistry anomalies?" |
| `silver.structure` | 4 (placeholder?) | "What's the dominant strike/dip?" / "Fault orientation?" |
| `silver.geophysics_surveys` | 1 (placeholder?) | "Was there a gamma anomaly?" / "Magnetic signature?" |
| `silver.geochronology_samples` | 0 | "When was this rock formed?" / "Age dating results?" |
| `silver.geological_formations` | 0 | "What formation is this part of?" |
| `silver.spatial_features` | 0 | "Outcrop / boundary / contact features" |
| `silver.historic_workings` | 0 | "Has this area been mined before?" |
| `silver.mineral_claims` | 0 | "Who holds the claims?" |
| `silver.qp_credentials` | 0 | "Who is the Qualified Person?" / "What are their qualifications?" |
| `silver.project_boundaries` | 0 | "What's the project area?" |

### Gold — all empty (views need to be materialised + data needs to land)
| Table | Status | Demo importance |
|---|---|---|
| `gold.assay_composites` | 0 | **CRITICAL** — QP-narrative intercepts ("12.5m at 0.45% U3O8") |
| `gold.significant_intersections` | 0 | **CRITICAL** — filtered intercepts at cutoff grades |
| `gold.drill_summaries` | 0 | HIGH — per-hole rollup ("Hole X intersected N significant zones") |
| `gold.drillhole_intervals_visual` | 0 | HIGH — visual interval representations |
| `gold.qaqc_statistics` | 0 | MEDIUM — aggregate QA stats per campaign/lab |
| `gold.element_correlations` | 0 | HIGH — element-vs-element relationships ("U3O8 correlates with Mo at r=0.65") |
| `gold.zone_statistics` | 0 | HIGH — by-zone aggregates ("Zone A averages 0.32% U3O8") |
| `gold.campaign_summaries` | 0 | MEDIUM — drilling campaign rollups |
| `gold.cross_section_panels` | 0 | LOW — pre-rendered cross-sections (no real text) |
| `gold.structure_measurements_visual` | 3 | (de-facto empty) |
| `gold.h3_density_mineral` | 133,282 | LOW — spatial density grid, hard to narrate as prose |

---

## 2. What we can synthesize TODAY (no new data needed)

These are populated silver tables where ADR-0012 hasn't yet shipped a synthesizer. Each one converts to NL passages immediately.

### Wave 2 synthesizers — proposed

| # | Table | Output | Demo value (1–5) |
|---|---|---:|---:|
| 1 | `silver.entity_aliases` | ~1,340 passages (batched 10 aliases per passage) | **5** |
| 2 | `public.smdi_deposits` | ~6,012 passages | **5** |
| 3 | `silver.samples` (stub upgrade) | ~4,379 passages | **5** |
| 4 | `silver.geological_ontology_terms` | 83 definitional passages | **5** |
| 5 | `silver.data_quality_flags` | ~220 passages (batched 10 per) | **3** |
| 6 | `silver.rock_codes` | 30 passages | **4** |
| 7 | `silver.campaigns` | 15 passages | **3** |
| 8 | `silver.geological_ontology_synonyms` | ~14 passages | **3** |
| 9 | `silver.reports.resource_estimate` JSONB | 64 narrative passages | **5** |
| **Total** | | **~11,800 new passages** | |

Add to ADR-0012 wave 1 (~12,000 passages) + existing 7,929 chunks =
**~31,700 passages total**, going from a prose-only corpus to one that
spans every geological topic a question could touch on.

### Sample synthesis previews

**`silver.geological_ontology_terms`** (commodity class):

> *"Uranium (commodity, atomic number 92) is a heavy radioactive metal occurring in nature primarily as the isotope U-238 (99.27%) and U-235 (0.72%). In geological exploration, uranium grade is conventionally reported as U3O8 (triuranium octoxide). Key uranium deposit models: unconformity-related (e.g. Athabasca Basin), sandstone-hosted, IOCG, calcrete, breccia complex. Cutoff grades typically range from 0.01% U3O8 (low-grade ISR) to 1.0% U3O8 (high-grade unconformity)."*

**`silver.entity_aliases`** (commodity class):

> *"In the geological literature, the following are aliases for the same concept: 'U3O8' (canonical, oxide form), 'triuranium octoxide' (full chemical name), 'yellowcake' (industrial form), 'uranium ore' (general), 'pitchblende' (mineral). When parsing reports, all of these refer to uranium content reported in oxide form."*

**`public.smdi_deposits`**:

> *"SMDI deposit ID 1247 (Patterson Lake South) located at UTM 13N 612345E 5734567N. Commodity: Uranium. Deposit type: unconformity-related uranium. Host rock: graphitic pelitic gneiss, Athabasca Group sandstone unconformity. Status: development. Operator: Cameco Corporation (50%), Orano Canada (50%). Discovered 2012. Historical significance: highest-grade unconformity uranium deposit in the Athabasca Basin."*

**`silver.samples`**:

> *"Sample PLS-2024-1247 from drillhole MAC-22-11 at depth 142.3-145.6m. Sample type: half-core (NQ-size, 47.6mm diameter). Length 3.3m, weight 4.2 kg. Taken on 2024-08-15 by site geologist J. Smith. Dispatched to SRC Geoanalytical 2024-08-22. Storage: bronze sample cabinet B-14 at the PLS core shack."*

**`silver.reports.resource_estimate` JSONB**:

> *"Patterson Lake South 2024 NI 43-101 Mineral Resource Estimate (effective date 2024-06-30, QP Robert Smith P.Geo., SRK Consulting). Indicated: 3.4 Mt grading 1.85% U3O8 (140.6 Mlbs U3O8 contained). Inferred: 1.2 Mt grading 1.25% U3O8 (33.1 Mlbs U3O8 contained). Cutoff grade 0.10% U3O8. Reported in accordance with CIM Definition Standards (2014)."*

### Gold layer — pre-build templates now (data flows later)

When `gold.assay_composites` / `gold.significant_intersections` / `gold.drill_summaries` start landing rows, the QP-narrative passages they synthesize are EXACTLY the textual representation geologists use when reading reports:

> *"Drillhole MAC-22-11 (Patterson Lake South) intersected 3 significant mineralised intervals: 12.5m at 0.45% U3O8 (142.3-154.8m, true width ~10.2m, peak 0.85% U3O8), 8.2m at 0.32% U3O8 (167.1-175.3m, true width ~6.8m), and 15.3m at 0.55% U3O8 (188.0-203.3m, true width ~12.5m). Total mineralised metres: 36.0m averaging 0.46% U3O8 above 0.10% U3O8 cutoff."*

This phrasing **IS** how NI 43-101 reports describe drillholes. Training the reranker on these passages directly teaches it to recognise the conversational equivalents geologists ask in chat.

---

## 3. The five demo-blocking data gaps

These tables are empty (or near-empty) and **synthesizers can't help** — the data must be ingested first.

| Table | Block | Suggested ingestion source |
|---|---|---|
| `silver.alteration` | "What's the alteration?" questions | Cameco PLS XLSX export — alteration sheet |
| `silver.mineralization` | "What's the mineralisation style?" | Cameco PLS XLSX export — mineralisation sheet |
| `silver.geochemistry` | "Rock geochem anomalies?" | PLS / Cigar Lake rock-chip CSV |
| `silver.structure` | "Strike/dip/lineation?" | PLS / Cigar Lake structure XLSX |
| `silver.geophysics_surveys` | "Gamma / magnetic signature?" | PLS geophysics survey LAS / CSV exports |

These exist in Cameco's data shipments — they just haven't been mapped into the silver tables yet. Each is a 1-2 day ingestion mapper.

**For the demo specifically**: if Cameco doesn't ship these by demo day, the model will have *good* coverage on drilling / assays / lithology / reports / textbook content, and *gaps* on alteration / mineralisation style / structure / geophysics. Phrase demo questions around what's covered.

---

## 4. Bronze coverage — what's actually missing

I had said earlier that bronze adds little because silver is the parsed-text subset. Re-audit:

* `bronze.ingest_manifest` — file metadata. Filename, SHA, MIME, size. **No domain content.**
* `bronze.provenance` — bronze→silver audit ledger. **No domain content.**
* Raw PDFs in MinIO — every PDF that successfully parsed exists as `silver.document_passages` chunks. **Failed/partial parses lose content, but the volume is small** (~5-10% of pages per MEMORY:project_pdf_coverage_overhaul).
* Raw CSVs/XLSX in MinIO — header rows + sheet names + cell comments are NOT in silver (silver normalises to typed columns). **This is the one real bronze textual gap.** Example: a Cameco assay XLSX has comments like *"Re-assayed due to original blank failure"* that go to silver.assays_v2.qaqc_flag='re_assayed' but the comment text is dropped.
* Raw LAS files in MinIO — LAS headers contain rich narrative commentary (~LAS_HEADER and ~CURVE blocks). Silver.las_curves (when populated) strips this.

**Bronze recovery candidates**:
1. **XLSX comments + sheet names** — write a small extractor that opens each bronze XLSX, pulls cell-level comments + sheet metadata into a `bronze.xlsx_commentary` table, and a synthesizer renders one passage per (file, sheet). Estimated 50-500 passages.
2. **LAS file headers** — extract ~LAS and ~CURVE block narrative text. Estimated 100-500 passages.
3. **CSV header rows** — typically `(sample_id, depth_from, depth_to, ...)`. Low-signal but completable. ~50-200 passages.

Combined bronze recovery: ~200-1,200 extra passages. **Low priority for demo realism but completable for v3 corpus.**

---

## 5. Recommended sequencing for the next training cycle

### Tier 1 — must ship before demo (4-6 hours of synthesizer work)
1. ADR-0012 wave 2 synthesizers:
   * `silver_samples_nl_summary` (was stub)
   * `silver_entity_aliases_nl_summary` (NEW)
   * `silver_smdi_deposits_nl_summary` (NEW — public.smdi_deposits cross-schema)
   * `silver_geological_ontology_terms_nl_summary` (NEW)
   * `silver_resource_estimates_nl_summary` (NEW — from silver.reports.resource_estimate JSONB)
2. Tests + commit + push
3. Materialise → +11,800 passages

### Tier 2 — should ship for richer training (3-4 hours)
4. `silver_rock_codes_nl_summary` (NEW)
5. `silver_data_quality_flags_nl_summary` (NEW)
6. `silver_campaigns_nl_summary` (NEW)
7. `silver_geological_ontology_synonyms_nl_summary` (NEW)

### Tier 3 — gold layer scaffolding (templates only, no data yet)
8. `gold_assay_composites_nl_summary` (template + tests, materialise when data lands)
9. `gold_significant_intersections_nl_summary`
10. `gold_drill_summaries_nl_summary`
11. `gold_element_correlations_nl_summary`
12. `gold_zone_statistics_nl_summary`

### Tier 4 — bronze recovery (low priority, post-demo)
13. XLSX commentary extractor + synthesizer
14. LAS header extractor + synthesizer
15. CSV header recovery

### After everything synthesizes
16. Re-run vocab extraction — will surface MUCH more domain vocab now that sample IDs, drill hole IDs, rock codes, deposit names, QA terms are whole-word frequent. Expect 5,000-10,000+ new vocab candidates (up from 195 in the current run).
17. Re-extend tokenizer with the new vocab.
18. Re-run MLM continued pretraining.
19. Re-run full reranker fine-tune.
20. Bench against an expanded golden set that includes alteration/mineralisation/structure questions — those will be the demo's weakest slices unless Cameco ships the missing data.

---

## 6. Data-side asks for Kyle (before demo)

* **Cameco PLS data shipment** — confirm we have / can get:
  * Alteration logs (`silver.alteration`)
  * Mineralisation style logs (`silver.mineralization`)
  * Rock geochemistry (`silver.geochemistry`)
  * Structural measurements (`silver.structure`)
  * Geophysics surveys (`silver.geophysics_surveys`)
* **Gold materialisation triggers** — `gold.assay_composites` and `gold.significant_intersections` need their Dagster assets materialised on the live dev DB. Probably a 30-minute job; high payoff for demo realism.
* **Public-geoscience extensions** — `silver.public_drillholes`, `silver.public_assessment_reports`, etc. (the BC MINFILE / USGS NGMDB / NRCan ones) are referenced in code but row counts unverified. If populated, those add tens of thousands of authoritative geological passages.

---

## 7. Estimated corpus size after each phase

| Phase | Passages | New training signal |
|---|---:|---|
| Today | 7,929 | NI 43-101 prose + Earle textbook |
| ADR-0012 wave 1 | ~20,000 | + assays + lithology + collars |
| **+ Wave 2 (this audit)** | **~31,700** | **+ samples + SMDI + KG aliases + ontology + resource estimates + QA + campaigns + synonyms + rock codes** |
| + Gold (when data lands) | ~37,000 | + QP-narrative composites + significant intercepts + drill summaries + element correlations |
| + Cameco missing data | ~45,000+ | + alteration + mineralisation + geochem + structure + geophysics |
| + Bronze recovery | ~46,500 | + XLSX comments + LAS headers + CSV headers |

The MLM training run currently in flight is on 7,929 passages. The corpus after Wave 2 + Cameco data + gold would be **6× larger and far more topically diverse** — the actual training-distribution-matches-inference-distribution we were hunting for in the LoRA cycle.

---

## 8. Demo realism checklist

Questions a geologist might ask, mapped to coverage state:

| Question | Tool path | Coverage today | Coverage after Wave 2 |
|---|---|---|---|
| "What was the U3O8 in PLS-22-11 around 142m?" | search_documents | ❌ — no chunk mentions this | ✓ — assay synthesis nails it |
| "What rock type is in this interval?" | search_documents | ❌ | ✓ — lithology synthesis |
| "What's the average grade across the hole?" | search_documents + gold | ❌ | ❌ until gold lands |
| "What's the dominant alteration?" | search_documents | ❌ | ❌ until Cameco ships data |
| "What's the structure look like?" | search_documents | ❌ | ❌ until Cameco ships data |
| "Who is the QP on this report?" | search_documents | ✓ (in NI 43-101 prose) | ✓ |
| "What's the resource estimate?" | search_documents | ✓ (in NI 43-101 prose) | ✓✓ (also synthesised standalone) |
| "Has anyone else explored this area?" | search_documents | ❌ | ✓ — SMDI synthesis |
| "What does 'pitchblende' mean?" | search_documents | partial | ✓ — KG alias synthesis |
| "What's an unconformity-related uranium deposit?" | search_documents | partial (textbook) | ✓ — CGI ontology synthesis |
| "When was this rock formed?" | search_documents | ❌ | ❌ until Cameco geochron data |
| "What's the project area?" | search_documents | partial (NI 43-101 prose) | partial (still no project_boundaries) |
| "Has this area been mined before?" | search_documents | ❌ | ❌ until historic_workings populated |

**Conclusion**: Wave 2 closes most of the conversational drilling/sampling/reporting gap. Demo realism is then bottlenecked by Cameco data shipment for alteration/mineralisation/structure/geophysics.
