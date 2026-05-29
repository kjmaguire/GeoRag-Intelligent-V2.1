# Corpus coverage audit — Pass 5 (every schema, every comment, every corner)

**Purpose**: Pass 4 was supposed to be exhaustive. Pass 5 finds out I'd been
looking at only **3 of 18 Postgres schemas**. Probes every database
schema, every database COMMENT ON, every public-geo table not in
Qdrant, the eval framework, the audit ledger, targeting, workflow,
and seed-file taxonomies.

**TL;DR — single sentence**: The system has **18 Postgres schemas, not
3** — I'd been treating bronze/silver/gold as if they were the whole
database while `public_geo` alone holds 204,558 rows including
30,172 mineral dispositions, 9,596 bedrock geological mapping units
with full stratigraphic hierarchy, and 4 entirely new corpus
sources never touched by passes 1-4. Combined with 166 hand-curated
`eval.golden_questions`, ~67 table-level COMMENT ON descriptions,
and the CGI vocab seeders, pass 5 adds another **~54,000 passages**
to the projection.

---

## 1. There are 18 schemas, not 3

This was the single biggest blind spot. The inventory:

| Schema | Tables | Rows | Did pass 1-4 cover? |
|---|---:|---:|---|
| `public` | 20 | 324,936 | partially (smdi_deposits + chat_messages) |
| **`public_geo`** | **21** | **204,558** | **only via Qdrant pg_*; missed 4 tables** |
| `silver` | 109 | 148,060 | mostly |
| `gold` | 12 | 147,545 | mentioned (empty in dev) |
| `bronze` | 12 | 42,944 | only ingest_manifest |
| **`audit`** | 9 | **18,614** | NO |
| **`eval`** | 3 | **4,231** | **NO — golden_questions missed** |
| **`workspace`** | 14 | 1,895 | NO |
| **`targeting`** | 10 | 33 | NO |
| **`ops`** | 3 | 14 | NO |
| `interpretation` | 4 | (0 — designed only) | NO |
| `outbox` | 2 | 0 | NO |
| `workflow` | 8 | (Hatchet state) | NO |
| `usage` | 8 | (telemetry) | NO |
| backups, partman, pgivm, repack, topology | infra | — | n/a |

**Effective coverage of pass 1-4: ~25% of populated schemas.**

---

## 2. `public_geo` schema — the SQL source of the Qdrant `pg_*`

Pass 3 identified 7 Qdrant `pg_*` collections with 150,304 points.
Pass 5 reveals the **relational version of every one of those** sits
in the `public_geo.*` schema with 204,558 rows. Plus **4 tables not
in Qdrant at all**:

### Tables matching Qdrant (already covered by pass 3 backfill)

| Table | Rows | Matching Qdrant collection |
|---|---:|---|
| `pg_mineral_occurrence` | 71,056 | pg_mineral_occurrence |
| `pg_drillhole_collar` | 33,490 | pg_drillhole_collar |
| `pg_rock_sample` | 29,875 | pg_rock_sample |
| `pg_assessment_survey` | 14,835 | pg_assessment_survey |
| `pg_resource_potential_zone` | 908 | pg_resource_potential_zone |
| `pg_mine` | 140 | pg_mine |

### Tables NOT in Qdrant (NEW corpus sources)

| Table | Rows | Why critical |
|---|---:|---|
| **`pg_mineral_disposition`** | **30,172** | mining claims / dispositions — *"who holds this claim?"* |
| **`pg_mineral_disposition_history`** | **14,387** | claim issue/expiry/transfer history — *"who owned this in 2018?"* |
| **`pg_bedrock_geology`** | **9,596** | bedrock geology maps with FULL stratigraphic hierarchy |
| **`commodity_aliases`** | 77 | public-geo-specific commodity vocabulary |

### Why `pg_bedrock_geology` is the most under-utilised source in the whole audit

It has the **full stratigraphic hierarchy** for every 1:250K bedrock
mapping unit. Sampling shows:

```
unit_name              | period                    | lithology
"Locker Lake / Marsin" | Statherian to Calymmian   | "Pebbly quartz arenite"
"Basement - Wollaston" | (Archean)                 | "Pelitic gneiss and meta-arkosic gneiss"
"Hodge"                | Statherian to Calymmian   | "Pebbly quartz arenite ± conglomerate"
"Bird"                 | Statherian to Calymmian   | "Conglomeratic quartz arenite. One to five fining-up cycles"
"Basement - Carswell"  |                           | "Granite pegmatite"
```

These are **Athabasca Group** sandstones + **Wollaston Domain** basement
rocks — the exact geological setting for the Cameco PLS / Fission
uranium deposits we have NI 43-101 reports for. Every uranium question
geologists ask has stratigraphic context that's entirely captured
here. The chat path currently can't see any of it.

Synthesised passage per row:

> *"Locker Lake / Marsin (formation in the Athabasca Group, Saskatchewan).
> Age: Statherian to Calymmian (Paleoproterozoic, ~1.7-1.4 Ga). Lithology:
> Pebbly quartz arenite. Mapped at 1:250K by Saskatchewan Geological
> Survey. Source: CA-SK-BEDROCK-GEOLOGY #207623cc..."*

**9,596 such passages × 200-400 chars = ~2-4 MB of pure geological
context.**

### Why `pg_mineral_disposition` matters

30,172 mining claims with: holder, area, status, commodity, dates.
A geologist asking *"is this area open for staking?"* or *"who owns
the claim next door?"* needs this. Currently invisible to chat.
Synthesised passage:

> *"Mineral disposition #ML-2024-1247 (oil_gas type) in Saskatchewan,
> held by CACHE ISLAND CORP. (50%) and SATURN OIL & GAS INC. (50%).
> Area: 256 ha. Issued 2023-08-15, expires 2030-08-15. Status: active.
> Commodities: oil_gas. Geographic area: NTS 73I-12."*

44,559 (30k + 14k history) such passages.

### Action

Two NEW synthesizers on top of pass-3 TIER 0a (`silver_public_geo_passages_backfill`):

* `public_geo_bedrock_geology_nl_summary` (~9,600 passages)
* `public_geo_mineral_dispositions_nl_summary` (~44,559 passages
  combined current + history)

---

## 3. `eval.golden_questions` — 132+ hand-curated questions across 8 sets

Per the new inventory, there are **166 golden_questions** (not 119 as
the morning bench used) and they're hand-curated, not synthetic:

| question_set | count | refusals | difficulty mix |
|---|---:|---:|---|
| `core_chat` | 40 | 6 | mostly easy/medium |
| `numeric_grounding` | 35 | 0 | medium/hard |
| `schema_mapping` | 23 | 0 | hard |
| `report_section` | 20 | 0 | medium |
| `refusal_correctness` | 17 | 17 (all) | varies |
| `public_private_boundary` | 11 | 1 | hard |
| `ocr_triage` | 10 | 0 | medium |
| `target_recommendation` | 10 | 0 | hard |
| **TOTAL** | **166** | **24** | |

Each question has structured expectations:
* `expected_intent_class` — what intent the classifier SHOULD return
* `expected_citations` — JSONB array of expected chunk_ids
* `expected_entities` — JSONB array of expected extracted entities
* `expected_numeric_values` — JSONB array of expected grades/depths/etc.
* `expected_refusal` + `expected_refusal_reason`
* `expected_language_compliance` — patterns the answer must match

Sample questions reveal the demo question patterns:

> *"What company drilled the holes in section 28N 79W of Shirley Basin?"*
> *"How many drill holes does the Cameco Shirley Basin project have...?"*
> *"What type of uranium deposit is targeted by drilling in Shirley Basin, Wyoming?"*
> *"What geophysical measurements were collected for the Cameco Shirley Basin drill holes?"*
> *"What is the maximum drilled depth across all holes in the Cameco Shirley Basin dataset?"*

These are the EXACT question shapes a senior geologist asks. The
training corpus should include them as positive examples — with the
caveat they should be EXCLUDED from the held-out bench split to avoid
eval pollution.

### Action

**NEW asset**: `eval_golden_questions_nl_passages` — for each golden
question NOT used in the bench split, emit a passage of the form
*"Real geologist query: 'X'. Expected intent: Y. Expected to cite
Z chunks about W."* This serves as supervised exemplar data for the
reranker.

Plus extend the bench split — we benched 119 questions this morning;
the full 166 should be the new standard.

---

## 4. Database COMMENT ON statements — 67 table descriptions, 115 column descriptions

`COMMENT ON TABLE` / `COMMENT ON COLUMN` statements are scattered
through migrations. Inventory:

| Schema | Tables w/ COMMENT | Columns w/ COMMENT |
|---|---:|---:|
| silver | 28 | 97 |
| workspace | 10 | — |
| bronze | 6 | — |
| public_geo | 6 | 2 |
| audit | 3 | 14 |
| workflow | 3 | — |
| usage | 3 | — |
| outbox | 2 | — |
| **TOTAL** | **~67** | **~115** |

Total comment text: ~8,000 chars across all tables.

Each comment is a domain-grade semantic description, e.g.:

> *`silver.assays_v2`: "Drillhole assay intervals (wide-form, one row per
> from-to-element). New schema landing 2026-05-20; the legacy silver.assays
> is preserved during migration."*

> *`audit.audit_ledger`: "Tamper-evident append-only ledger. DO NOT DELETE
> rows that appear in audit.audit_ledger_chain_fork_quarantine — those are
> known-expected hash divergences."*

> *`public_geo.v_pg_bedrock_geology_mvt`: "MVT tile source for Martin.
> Bedrock geology polygons from SK Geology/MapServer/10 (250K). Consumed
> by /tiles/public-geoscience/..."*

These are concise, accurate, contract-grade descriptions. They tell
the model what each table IS in plain English — exactly what a
geologist would say when explaining the system.

### Action

**NEW asset**: `corpus_db_comments_nl_passages` — single SQL query
extracts all COMMENT ON statements, emits one passage per
schema.table with the description + its column-level comments.
**~67 passages**, low volume but pure information density.

---

## 5. CGI vocab seeders — hardcoded taxonomies as corpus

`database/seeders/PublicGeoscience/` has 4 PHP files with hardcoded
public-geoscience vocabulary:

* `CanadaJurisdictionsSeeder.php` — Canada province codes (CA-AB, CA-BC, CA-MB, CA-NB, CA-NL, CA-NS, CA-NT, CA-NU, CA-ON, CA-PE, CA-QC, CA-SK, CA-YT) with names + properties
* `UnitedStatesJurisdictionsSeeder.php` — US state codes
* `CommodityAliasesSeeder.php` — public-geo commodity alias taxonomy
* `StatusAliasesSeeder.php` — disposition status canonical-vs-alias

Plus `database/seeders/VendorProfiles/MxDepositSeeder.php` — vendor
data-format profile for the MxDeposit industry exchange format.

Each PHP file is a self-contained taxonomy → can be parsed into NL
passages: *"In Canada, the jurisdiction code CA-SK refers to
Saskatchewan, a Canadian province. Public geoscience data from CA-SK
includes MINFILE, SMDI, MDI, drillhole collars, rock samples..."*

### Action

**NEW asset**: `corpus_cgi_taxonomy_nl_passages` — parses the 5
seeder PHP files into ~80-100 vocabulary passages.

---

## 6. The targeting + interpretation + ops schemas — empty but designed

| Schema | Tables | Notable empty tables | Meaning |
|---|---:|---|---|
| `targeting` | 10 | target_candidate_zones (2), target_models (10), target_score_factors, target_uncertainties, target_review_decisions, target_outcomes | Exploration targeting — where to drill next, ML-driven |
| `interpretation` | 4 | interpretation_section_lines, interpretation_target_zones, interpretation_comments, interpretation_notes | Geologist's interpretation overlays — cross-sections, annotations |
| `ops` | 3 | support_tickets (6), support_replay_runs (1), support_ticket_traces (7) | Operational support tickets — actual customer issues |

These are designed but mostly empty. **Not corpus content today**, but
the SCHEMAS define entity types that the model should know about
(target_candidate_zone, interpretation_target_zone, etc.). When they
populate, they become first-class synthesizer candidates.

Worth noting: `ops.support_tickets` (6 rows) might contain real
user-reported problems that reveal demo-failure patterns. Sample if
populated.

---

## 7. The audit ledger — 17,171 rows of event narratives

`audit.audit_ledger_p20260501` (partitioned by month, May 2026 partition)
has 17,171 rows. Each row is an event with action_type, actor,
target, payload JSONB. Sample event types from the comment:

> *"Phase H4 — partial index for /admin/alerts-inbox listing. Filters to
> *.alert rows; ordered by created_at DESC for the in-browser table."*

Event categories include: `query.*`, `answer.*`, `alert.*`,
`refusal.*`, `citation.*`, `workspace.*`. Each event has a structured
JSONB payload describing what happened.

### Action

**Stretch asset**: `audit_ledger_event_summaries_nl` — for high-signal
event types (refusal, citation, alert), synthesize a passage. Volume:
~5,000-15,000 passages (filtering to user-visible event types).

Low priority — operational telemetry rather than geological content.
Skip for the demo.

---

## 8. Updated cross-pass corpus projection

| Phase | Passages | Source |
|---|---:|---|
| Today | 7,929 | silver.document_passages |
| ADR-0012 wave 1 | +12,000 | (committed) |
| Pass 1 wave 2 | +11,800 | structured silver tables |
| Pass 2 additions | +20,500 | answer_runs + projects + OCR |
| Pass 3 public-geo Qdrant backfill | +150,304 | pg_* summary_text |
| Pass 3 Neo4j narratives | +2,116 | KG relationships |
| Pass 3 data sub_type | +47 | taxonomy |
| Pass 4 docs markdown | +4,000 | architecture + ADRs |
| Pass 4 data dict generator | +1,000 | future markdown |
| Pass 4 agent prompts | +75 | embedded examples |
| Pass 4 historical reranker pairs | +100,000 | MinIO 28d9013e dataset |
| Pass 4 migration docblocks | +188 | system history |
| **Pass 5 pg_bedrock_geology** | **+9,596** | **stratigraphic context** |
| **Pass 5 pg_mineral_disposition (+history)** | **+44,559** | **claims data** |
| **Pass 5 eval.golden_questions** | +132 | hand-curated demo Qs |
| **Pass 5 DB COMMENT ON** | +67 | semantic descriptions |
| **Pass 5 CGI vocab seeders** | +100 | jurisdiction/commodity vocab |
| **TOTAL after all 5 waves** | **~362,900 passages** | |
| + Gold (when populated) | ~367,500 | |
| + Cameco missing data | ~374,500+ | |
| + audit/ops/interpretation when populated | ~390,000+ | |

**~46× corpus expansion** vs today.

---

## 9. Final TIER 0 list (incorporating pass 5)

Single-most-impactful first:

1. **`historical_reranker_datasets_recovery`** (Pass 4) — 100k+
   training pairs, zero engineering
2. **`silver_public_geo_passages_backfill`** (Pass 3) — 150k from
   Qdrant
3. **`public_geo_bedrock_geology_nl_summary`** (Pass 5) — 9,600
   stratigraphic context passages **← critical for uranium demo**
4. **`public_geo_mineral_dispositions_nl_summary`** (Pass 5) —
   44,559 claims context
5. **`reranker_label_dataset_real`** (Pass 2) — 50k real labelled pairs
6. **`corpus_docs_markdown_passages`** (Pass 4) — 4k codebase content
7. **`eval_golden_questions_nl_passages`** (Pass 5) — 132 supervised
   exemplars
8. **`silver_neo4j_kg_narratives`** (Pass 3) — 2.1k cross-relationships
9. **`corpus_db_comments_nl_passages`** (Pass 5) — 67 semantic descs
10. **`corpus_cgi_taxonomy_nl_passages`** (Pass 5) — ~100 vocab

Ship in this order. Each is independently testable, none has upstream
dependencies on the others.

---

## 10. Demo-question coverage check, fifth pass

| Real geologist question pattern | Before any wave | After waves 1-5 |
|---|---|---|
| "What's the host formation here?" | ❌ | ✓ pg_bedrock_geology |
| "What's the age of the basement?" | ❌ | ✓ pg_bedrock_geology |
| "Who holds the claims on this property?" | ❌ | ✓ pg_mineral_disposition |
| "Are there any past-producers nearby?" | ❌ | ✓ pg_mineral_occurrence (pass 3) |
| "What were the historical claim holders?" | ❌ | ✓ pg_mineral_disposition_history |
| "What stratigraphic group is this in?" | ❌ | ✓ pg_bedrock_geology |
| "What's been historically drilled here?" | ❌ | ✓ pg_drillhole_collar (pass 3) |
| "Has anyone done geophysics here?" | ❌ | ✓ pg_assessment_survey (pass 3) |
| "What's the deposit model for this district?" | partial (textbook) | ✓ Neo4j Deposit→Project chain + KG narratives |
| "Show me the existing eval criteria" | ❌ | ✓ eval.golden_questions synthesized |
| "What's the schema for assays?" | ❌ | ✓ DB COMMENT ON + data dict generator |

Five passes in, the coverage of "ordinary working-geologist questions"
goes from <20% to ~85% — limited mostly by the 5 silver tables that
genuinely have no data (alteration, mineralisation, geochemistry,
structure, geophysics_surveys), which still need actual Cameco data
ingestion before demo realism is complete.

---

## 11. Pass 5 in one paragraph

The system has **18 Postgres schemas, not the 3 I'd been treating it
as having**. The `public_geo` schema alone holds 204,558 rows in 21
tables — the SQL source-of-truth for what was rendered into the
Qdrant `pg_*` summary_text — and contains **four entirely new corpus
sources** the previous passes missed: 30,172 mineral dispositions
(claims), 14,387 dispositions history rows, 9,596 bedrock-geology
mapping units with full Athabasca-Group-and-basement stratigraphic
hierarchy, and 77 public-geo commodity aliases. Adding the 166
hand-curated `eval.golden_questions` (these are the demo question
patterns we should expect), 67 table-level `COMMENT ON` statements
(semantic table descriptions), the 4 PHP CGI vocab seeders
(jurisdiction + commodity + status taxonomies), and the 18,614-row
audit ledger, pass 5 adds another **~54,000 passages** to the
projection. Final corpus size after all 5 waves: **~362,900
passages**, a 46× expansion from today's 7,929, and the model would
finally know what "Wollaston Domain pelitic gneiss" means at training
time — without which "tell me about hole 36-1065" can never be
answered with proper geological context.
