# Corpus coverage audit — Pass 6 (100% coverage push)

**Purpose**: Kyle asked for the deepest pass possible — 100% coverage,
every corner. Pass 5 covered the 18 schemas inventory. Pass 6 goes
inside each schema's *internals*: functions, check constraints,
triggers, RLS policies, materialized views, the data quality
findings, the frontend components, the test suites as encoded
domain scenarios.

**TL;DR — single sentence**: The database alone carries **1,732
PostgreSQL functions, 299 CHECK constraints, 16 triggers, 142 RLS
policies, 168 views, 16 extensions, and 2,215 actual data-quality
findings** with rule names, severities, and descriptions — each
function body / constraint expression / trigger purpose / policy
predicate / view definition IS encoded domain knowledge, and the
frontend has 14+ first-class geological visualization components
(AlterationMap, REESpider, PCAOxides, MultiHole3DTrace) plus 381
test files each encoding a complete domain scenario.

---

## 1. Database internals — every category at a glance

| Category | Count | Domain content per item |
|---|---:|---|
| Functions | **1,732** | Function body source = procedural geology |
| Check constraints | **299** | Encoded domain invariants |
| Triggers | **16** | Silent business logic |
| Foreign keys | **247** | Relationship graph (the real schema) |
| Views | **168** | Curated geological aggregations |
| Materialized views | 1 (more in gold when populated) | Pre-computed rollups |
| Enums | 4 | Domain type definitions |
| RLS policies | **142** | Workspace tenancy patterns |
| Extensions | **16** | PostGIS, pgcrypto, pg_trgm, H3, partman, ... |

Each line in this table contains semantic information about what
the system models. Together they're a complete *executable* domain
specification — written in DDL but readable as prose.

---

## 2. CHECK constraints = encoded domain rules

299 CHECK constraints, each a single line of SQL that pins a
geological invariant. A sampling reveals the canon:

```
silver.lithology_logs.chk_litho_depth_order    →  from_depth < to_depth
silver.samples.chk_sample_depth_order          →  from_depth < to_depth
silver.lithology_logs.chk_rqd_range            →  rqd BETWEEN 0 AND 100
silver.lithology_logs.chk_recovery_range       →  recovery BETWEEN 0 AND 100
silver.collars.chk_total_depth_positive        →  total_depth > 0
silver.collars.chk_elevation_range             →  elevation BETWEEN -500 AND 9000
silver.collars.chk_azimuth_range               →  azimuth BETWEEN 0 AND 360
silver.collars.chk_dip_range                   →  dip BETWEEN -90 AND 0
silver.seismic_surveys.survey_type_check       →  survey_type IN ('2D', '3D')
silver.projects.projects_status_check          →  status IN ('active', 'indexing', 'decommissioned', ...)
public_geo.jurisdictions.jurisdictions_level_check  →  level IN ('country', 'province', 'territory', 'state')
silver.geological_ontology_terms.class_valid   →  class IN ('deposit_model', 'commodity', 'lithology', 'alteration', ...)
```

Each is a single sentence of geological truth: *"drillhole dip must be
between -90 and 0 degrees"*, *"recovery is a percentage between 0 and
100"*, *"a seismic survey is either 2D or 3D"*.

### Action

**NEW asset**: `corpus_check_constraints_nl_passages` — for each
constraint in domain schemas, emit a passage:

> *"Constraint silver.collars.chk_dip_range: A drillhole's dip angle
> must be between -90° and 0°. Values are reported as negative numbers
> indicating downward from horizontal. A vertical hole is dip = -90°;
> a horizontal hole is dip = 0°. This rule enforces standard mining
> industry convention for drillhole orientation."*

**~299 passages.** High information density per token.

---

## 3. `silver.data_quality_flags` — 2,215 actual data quality findings

Pass 1 noted 2,215 rows but didn't open the table. Pass 6 reveals the
rule breakdown:

| rule_id | severity | flagged_rows |
|---|---|---:|
| `collar.missing_dip` | INFO | 547 |
| `collar.missing_elevation` | WARNING | 434 |
| `collar.missing_azimuth` | INFO | 434 |
| `collar.geom_missing_with_coords` | ERROR | 265 |
| `collar.crs_assumed` | WARNING | 265 |
| `collar.crs_low_confidence` | WARNING | 265 |
| `assay.interval_overlap` | WARNING | 5 |

**Each flag is a row-level narrative**: record_id, source_document_id,
source_page, description, threshold_payload (JSONB with rule
parameters), flagged_at.

Sample synthesised passage:

> *"Data quality finding: drillhole MAC-22-11 (record_id b0000000-...,
> from project Patterson Lake South, source report 645d8e22-... page 14).
> Rule collar.missing_elevation v1.0 (WARNING level). Description:
> Collar row reports easting + northing but elevation is NULL.
> Cross-check against published collar table or topographic surface
> required. Threshold: elevation IS NOT NULL when easting/northing
> populated. Flagged 2026-05-22 14:23 UTC by silver_collar_dq rule pack."*

These passages teach the model **what data quality issues look like
in real geological datasets** — exactly what a geologist would
recognise when reviewing a hand-off package from a contractor.

### Action

**NEW asset**: `corpus_data_quality_flags_nl_passages` — one passage
per flag. **~2,215 passages.**

---

## 4. Database functions — 1,732 procedural-geology pieces

The 1,732 function count is misleading because ~80% are H3 raster
internals (skip those). The signal-bearing ones cluster into:

| Function family | Approx count | What they describe |
|---|---:|---|
| H3 raster operations | ~1,400 | spatial bucketing — skip |
| MVT tile generators | 8 | map layer publish points (pass 5 — already covered) |
| Coverage / density | ~10 | per-project coverage calculations |
| Trigger functions | 16 | (see §6 below) |
| Validation rules | ~30 | CRS / unit / overlap / range validators |
| Spatial transformations | ~50 | ST_GeomFromText wrappers, projection helpers |
| Aggregation helpers | ~30 | per-project / per-hole aggregations |
| **Domain-signal functions** | **~150** | named geological computations |

Each domain-signal function has a name like `silver.coverage_density(...)`,
`silver.compute_true_thickness(...)`, `gold.recalculate_zone_grade(...)`.

The function **body source** describes the computation step-by-step in
SQL. Each is 10-50 lines of geological algorithm. Extracting them
into prose passages teaches the model the system's own computation
vocabulary.

### Action

**Stretch asset**: `corpus_db_function_signatures_nl_passages` — for
~150 domain functions, emit a passage describing what the function
computes. Limited volume but each adds dense procedural knowledge.

---

## 5. Foreign keys — 247 relationships defining the schema graph

247 FK relationships across domain schemas. Each FK is a graph edge
of the form `(child_table.column → parent_table.column)`. These
*are* the schema:

```
silver.lithology.collar_id        →  silver.collars.collar_id
silver.assays_v2.collar_id        →  silver.collars.collar_id
silver.samples.collar_id          →  silver.collars.collar_id
silver.projects.workspace_id      →  silver.workspaces.workspace_id
silver.document_passages.document_id  →  silver.reports.report_id
gold.assay_composites.collar_id   →  silver.collars.collar_id
public_geo.pg_mineral_disposition_history.disposition_id  →  public_geo.pg_mineral_disposition.id
```

Each FK is a sentence-level domain truth: *"every lithology row
belongs to a drillhole"*, *"every assay composite belongs to a
drillhole"*, *"every disposition history row references its parent
disposition"*.

### Action

Synthesize the FK graph as relationship narratives — *"In the GeoRAG
schema, a drillhole (silver.collars) is the central anchor: lithology,
samples, assays, and gold composites all reference it via collar_id..."*
Single asset, ~50-100 multi-relationship passages.

---

## 6. Triggers — 16 silent business-logic narratives

16 triggers across domain schemas. Each one captures a domain rule
that fires on insert/update. From migration history:

* `bronze_provenance_workspace_id_autofill` — auto-populates workspace_id
  on bronze.provenance from the linked silver target. (Landed 2026-05-25.)
* `silver_reports_set_data_version` — bumps workspace data_version on
  insert. (Per ADR.)
* RLS-enforcing triggers — block writes that violate workspace tenancy.

Each trigger is a *silent business rule* the model should know about
when reasoning about data flow.

### Action

Stretch asset: `corpus_db_triggers_nl_passages` — extract each
trigger's source + its purpose comment, emit one passage per trigger.
**~16 passages.**

---

## 7. RLS policies — 142 workspace tenancy patterns

Pass 1 noted RLS exists; pass 6 counts 142 policies across domain
schemas. Each policy is a USING clause that gates row visibility.

The pattern is consistent: `workspace_id = current_setting('app.workspace_id')`,
with variations for cross-workspace shared resources (CGI vocab,
public_geo).

Not corpus content per se — but documenting these as 1-2 passages
per schema explains the tenancy model. **~12 passages** (one per
schema).

---

## 8. Frontend components — first-class geological visualizations

`resources/js/Components/` (96 pages — pass 4 noted) has at least 14
geology-specific visualization components:

| Component | What it visualizes |
|---|---|
| `Admin/TrgZoneMap.tsx` | Targeting candidate zone overlay |
| `Analytics/AlterationMap.tsx` | Alteration intensity map |
| `Analytics/GradeDistribution.tsx` | Grade distribution histograms |
| `Analytics/MultiHole3DTrace.tsx` | 3D drillhole traces with assay colour-coding |
| `Analytics/PCAOxides.tsx` | Principal Component Analysis on major oxides |
| `Analytics/REESpider.tsx` | Rare Earth Element spider diagrams |
| `DrillTrace3D.tsx` | Single-hole 3D trace |
| `Foundry/WorkspaceMap.tsx` | Workspace overview map |
| `PublicGeoscience/PublicGeoscienceMap.tsx` | Public-geo overlay |
| `CoverageTableCard.tsx` | Per-domain coverage rollup |
| `StereonetCard.tsx` | Structural-geology stereonet |
| `TimelineCard.tsx` | Project timeline |
| `chat/CitationMarker.tsx` | Inline citation chips |
| `chat/RefusalPanel.tsx` | Refusal explanations |
| `chat/ConflictCards.tsx` | Conflicting evidence cards |

Each component file has:
* JSDoc header describing what it shows
* PropTypes documenting the geological data it expects
* In-component strings labeling axes, legends, controls

PCAOxides + REESpider especially encode **advanced exploration
geochemistry** vocabulary that doesn't appear elsewhere in the
corpus.

### Action

**NEW asset**: `corpus_frontend_components_nl_passages` — extract the
header docstring + main JSX text strings from each component, emit
one passage per component file. **~96 passages**, each densely
domain-flavored.

---

## 9. Test suites as encoded domain scenarios — 381 files

`tests/Feature/`, `src/fastapi/tests/`, `src/dagster/tests/`
collectively hold 381 test files. Each test:

1. Sets up a domain scenario (mock workspace, sample data)
2. Performs a domain action (ingest, query, classify)
3. Asserts a domain outcome (chunks match, citations valid)

Test bodies contain:
* Real-shape geological data (sample IDs, hole IDs, formations)
* Real query strings ("What's the grade in PLS-22-11?")
* Expected response patterns (citations, refusals)

Extracting test names + their docstrings would yield 381 micro-scenarios
each describing what the system should do in a specific case.

### Action

**Stretch asset**: `corpus_test_docstrings_nl_passages` — pytest +
PHPUnit docstring extraction. **~381 passages.** Each one is a
domain capability assertion: *"Test: when a user uploads a CSV with
column 'Au_ppm', the lithology parser must classify the column as
gold-in-ppm and emit silver.assays_v2 rows with element='Au' unit='ppm'."*

---

## 10. Extensions — 16 capabilities encoded as installed software

`SELECT extname FROM pg_extension;` — 16 extensions including:
* `postgis` — spatial geometry types + functions
* `postgis_topology` — topological operations
* `h3` — H3 hexagonal grid
* `pg_partman` — partitioned tables (audit ledger, usage events)
* `pg_trgm` — trigram text similarity
* `pgcrypto` — encryption (audit ledger HMAC)
* `pgivm` — incremental view maintenance
* `pg_repack` — table maintenance

Each extension is a domain capability the system provides. Worth a
single passage documenting "the GeoRAG database stack uses these 16
extensions for spatial, partitioning, encryption, and indexing
operations." Volume: 1.

---

## 11. Updated cross-pass corpus projection

| Phase | Passages |
|---|---:|
| Today | 7,929 |
| Waves 1-5 (cumulative) | +355,000 |
| Pass 6 — check constraints | +299 |
| Pass 6 — data quality findings | +2,215 |
| Pass 6 — DB functions (~150 domain-signal) | +150 |
| Pass 6 — foreign key narratives | +100 |
| Pass 6 — triggers | +16 |
| Pass 6 — RLS schema notes | +12 |
| Pass 6 — frontend components | +96 |
| Pass 6 — test docstrings | +381 |
| **TOTAL after all 6 passes** | **~366,200 passages** |

**~46× corpus expansion** vs today, with each pass-6 addition adding
*procedural / invariant / definitional* knowledge that the prose-only
corpus today can't express.

---

## 12. Final TIER ordering after 6 passes (single most impactful first)

1. `historical_reranker_datasets_recovery` (Pass 4) — 100k pairs, ZERO eng
2. `silver_public_geo_passages_backfill` (Pass 3) — 150k Qdrant
3. `public_geo_bedrock_geology_nl_summary` (Pass 5) — 9.6k strat context
4. `public_geo_mineral_dispositions_nl_summary` (Pass 5) — 44.5k claims
5. `reranker_label_dataset_real` (Pass 2) — 50k real labels
6. `corpus_docs_markdown_passages` (Pass 4) — 4k codebase
7. **`corpus_data_quality_flags_nl_passages` (Pass 6) — 2.2k DQ rules**
8. `silver_neo4j_kg_narratives` (Pass 3) — 2.1k KG cross-rel
9. `eval_golden_questions_nl_passages` (Pass 5) — 132 supervised
10. **`corpus_frontend_components_nl_passages` (Pass 6) — 96 viz docs**
11. **`corpus_test_docstrings_nl_passages` (Pass 6) — 381 scenarios**
12. **`corpus_check_constraints_nl_passages` (Pass 6) — 299 invariants**
13. **`corpus_fk_relationship_narratives` (Pass 6) — 100 schema-graph**
14. `corpus_db_comments_nl_passages` (Pass 5) — 67 semantic descs
15. `corpus_cgi_taxonomy_nl_passages` (Pass 5) — ~100 vocab
16. **`corpus_db_function_signatures` (Pass 6) — 150 procedural**
17. **`corpus_db_triggers_nl_passages` (Pass 6) — 16 silent rules**

---

## 13. Pass 6 in one paragraph

The DATABASE itself is a complete domain specification I'd been
treating as a content-only store. It carries **1,732 PostgreSQL
functions** (~150 of which are domain-signal computations), **299
CHECK constraints** (each a sentence-level geological invariant
like *"drillhole dip is between -90° and 0°"*), **2,215 actual data
quality findings** with rule names + severity + descriptions, **16
triggers** of silent business logic, **142 RLS policies** defining
workspace tenancy patterns, **247 foreign keys** encoding the
relationship graph, **168 views** (curated aggregations), and **16
extensions** documenting installed capabilities. The frontend has
**14+ first-class geological visualization components** including
PCAOxides + REESpider + AlterationMap + MultiHole3DTrace that
encode advanced exploration-geochemistry vocabulary appearing
nowhere else in the system. And the test suites encode **381
complete domain scenarios** — each one a setup-action-assertion
narrative of what the system does in a specific case. The corpus
projection now stands at **~366,200 passages**, a 46× expansion,
with pass-6 additions adding the *procedural / invariant /
definitional / capability* knowledge that no purely
content-extraction approach can produce.
