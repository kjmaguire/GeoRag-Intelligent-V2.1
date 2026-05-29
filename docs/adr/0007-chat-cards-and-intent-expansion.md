# ADR 0007: Chat-embedded interactive cards + two new agentic-retrieval intents

- **Date**: 2026-05-25
- **Status**: Accepted (approved by Kyle 2026-05-25)
- **Deciders**: Kyle Maguire (SME)
- **Supersedes**: nothing. Extends §04g (Visualization Specifications) and §04j (Agentic Retrieval LangGraph).
- **Related**: [ADR-0006](0006-agentic-retrieval-single-graph.md) (1 graph + 6 routed intents), `src/fastapi/app/agent/agentic_retrieval/intent_classifier.py`, `src/fastapi/app/agent/agentic_retrieval/state.py`, `src/dagster/georag_dagster/assets/silver_structure_derive.py`, `src/dagster/georag_dagster/assets/gold_structure_measurements_visual.py`

## Context

Kyle asked for two capabilities the chat surface does not currently answer:

1. **Project data-collection breakdown** — "what techniques, by which contractor, in what year, by which geologist." This is a structured aggregation, not free-text RAG.
2. **Data gap analysis** — "where are the holes in what we've collected for this project." Also structured: a coverage computation across spatial / temporal / technique / depth axes.

He also asked the chat to render maps, timelines, drillhole strips, stereonets, and 3D drillhole views *inline* — clickable, scrubbable, and self-contained inside the chat column.

A 2026-05-25 schema audit found:

- The **schema is already sized for this work**. `gold.drillhole_intervals_visual` (11,294 rows, populated) carries pre-computed `assay_payload`, `alteration_payload`, `structure_payload`, `color_hint`, and `visual_y_start/end`. `gold.structure_measurements_visual` carries `stereonet_x`, `stereonet_y`, `projection`. `gold.cross_section_panels` carries pre-projected collars + azimuth. Someone already designed for chat-card rendering.
- The **producers are partially built and largely idle**. `silver_structure_derive.py` is a 2nd-stage α/β → true_dip transformer; it correctly finds zero work because the **1st-stage populater** never wrote raw `silver.structure` rows. `silver_drill_traces` (geometry builder) and `gold_structure_measurements_visual` are wired but unpopulated for the same upstream-empty reason.
- The **breakdown-relevant columns exist but are 0% populated**: `silver.campaigns.contractor`/`geologist` (0/15), `silver.collars.driller`/`geologist` (0/567), `silver.assays_v2.lab_name` (0/540), `silver.reports.qp_name` (0/1209). Schema is right; no writer fills them.
- The **gap-analysis primitives exist but are barely used**: `silver.completeness_findings` (2 rows), `silver.corpus_health_findings` (0), `silver.document_ingestion_quality` (16). Strong baseline gap signal is the `bronze.ingest_manifest` (39,744 indexed files) vs `silver.reports` (1,209) ratio — ~97% of indexed files never produced a report row.

§04g already constrains the rendering tech: Stereonet via `mplstereonet` (server-side matplotlib), Drillhole strip log via Plotly, Map view via MapLibre GL + GeoJSON for chat-inline (tiles only for full Lakehouse view). §04j (after ADR-0006) routes 6 intents inside one `StateGraph.execute_node` — the new intents extend that table, not the graph shape. §04i requires every claim to carry a `source_chunk_id` or row-reference; card payloads inherit the same constraint.

A separate doc inconsistency exists in §04h, which still lists the pre-ADR-0006 intent set (`factual, spatial, document, computation, viz, unknown`). That's out of scope for this ADR — flagged for a separate one-line doc fix.

## Decision

Adopt all five of the following as one architectural extension:

1. **Extend the §04j intent set from 6 to 8.** Add `project_summary` and `coverage_gap` to `INTENT_LABELS` in `intent_classifier.py`. Both route inside the existing `execute_node`; no graph refactor. Each gets its own `retrieval_profile` entry (SQL-aggregate-first, vector-secondary) and its own OIUR prompt section.

2. **Formalize the chat-card envelope.** Extend `agentic_retrieval/state.py` with typed payload kinds: `map_payload` (already in §04g, formalized), `timeline_payload`, `drillhole_payload`, `stereonet_payload`, `drillhole3d_payload`. Each payload carries a `source_row_ids: list[uuid]` field anchored to the silver/gold rows it summarizes — the §04i citation contract applied to structured-data answers.

3. **Build the missing 1st-stage structural extractor.** A new Dagster asset `silver_structure_populate` parses α/β + strike/dip from `silver.lithology_logs.notes` and `silver.reports.sections_text` structural sections into `silver.structure`. Triggers the existing `silver_structure_derive` (α/β → true_dip) and `gold_structure_measurements_visual` cascade automatically. Unblocks the stereonet card with no new compute path.

4. **3D drillhole card stays on Plotly, not @react-three/fiber.** Discovered post-draft (2026-05-25): `resources/js/Components/DrillTrace3D.tsx` already exists, already ships in `InlineViz.tsx` under `chart_type='drill_trace_3d'`, and renders via Plotly's 3D scatter — no three.js in `package.json`. The original ADR proposal to pre-approve r3f was based on incomplete recon. Keeping Plotly is also better for stack consistency (every other chat-inline chart already uses it) and avoids a new dependency. PR-4 reduces to: trigger `silver_drill_traces` to populate the table, then optionally enhance the existing card with downhole-curve overlay once `silver.surveys` has data.

5. **Server-side stereonet rendering, not client-side.** Per §04g, `mplstereonet` produces the projection on the FastAPI worker; the card payload carries a base64 PNG + the underlying point list (for hover/click → cite). Equal-area (Schmidt) is the default projection (SME confirmation deferred to PR-2). Keeps the citation chain intact — the LLM never invents stereonet points.

## Rationale

1. **Schema-side cost is near zero.** No new tables, no new columns. Two intent labels + five typed payload shapes is small.
2. **Citation contract holds.** Every payload references `silver.*` / `gold.*` rows by id. The §04i guards run unchanged — numerical claims in the card body still verify against the bound row set. Card visuals can't "hallucinate" because they render rows, not LLM text.
3. **Decoupled extraction work.** PR-2 (structural extractor) and PR-3 (contractor/geologist NER backfill) ship independently of the intent + card UI. PR-1 can ship the intent classifier + 3 of 5 cards (map, timeline, drillhole) on data that's already populated, and call out the missing dimensions in the answer body. Matches §04i "the refusal path is the product" — better to ship an honest partial answer than to hold for completeness.
4. **Reuses existing visual gold layer.** `gold.drillhole_intervals_visual` already has color hints, depth ranges, and stacked payloads — the drillhole card is a thin renderer over a pre-aggregated row. No new computation per query.
5. **Extends, doesn't supersede, ADR-0006.** Still one `StateGraph`. Still `classify → route → execute → assemble → validate → demote → persist`. New intents are new entries in the same routing table.

## Consequences

### Positive

- Two new high-value query classes (`project_summary`, `coverage_gap`) become first-class instead of falling into the synthesis catch-all where they currently produce hand-wavy answers.
- The gold `*_visual` tables — clearly built for this purpose — get their consumer. Today they're computed but unread.
- Adding `silver_structure_populate` unblocks four downstream surfaces (silver.structure, gold.structure_measurements_visual, gold.cross_section_panels structural overlays, the structural-context tools in agentic retrieval) — not just the stereonet card.
- The card envelope contract makes it cheap to add future card kinds (cross-section, geochem plots) — they slot into the same `payload` mechanism with their own source_row_ids.

### Negative

- The intent set grows. Each new intent is one more thing to keep in sync across `intent_classifier.py`, `retrieval_profile.py`, prompt sections, and the §04j table. ADR-0006 already accepted this cost shape.
- (Removed — react-three-fiber not adopted; see Decision item 4.)
- PR-1 ships with explicit "contractor / geologist not extracted" rows in `project_summary` answers until PR-3 backfills. Geologists see honest gaps for ~1 week between PR-1 and PR-3.
- Server-side stereonet rendering (matplotlib in a FastAPI worker) is a CPU cost on every query that returns a stereonet card. Cap card-renders per answer to 1 stereonet, cache by `(workspace_id, project_id, structure_filter_hash)` for 1 h.

## Options considered

| Option | Shape | Why rejected |
|---|---|---|
| **Adopted: extend §04j intent set + card envelope; build missing extractor; pre-approve r3f** | 8 intents, 5 payload kinds, 4 sequenced PRs, 1 new frontend dep gated through this ADR. | Smallest change that delivers all three of Kyle's asks. Reuses the schema work already done. Honest staging via PR-1 partial-data shipping. |
| Build a separate `gold.project_summary_rollup` materialized view + serve via a non-LLM REST endpoint | Skip the intent route entirely; project summary is "just a chart in the Lakehouse tab." | Loses chat-context flow. User would have to ask the question, then go to a different tab to see the answer. Chat needs to *be* the answer surface per §04h evidence-binding goals. |
| Make stereonet client-side via a JS lib (`stereonet-js`, d3) | Skip the matplotlib worker call; render in browser. | Breaks the §04i citation chain (LLM/JS would have to assemble the projection from raw α/β, opening a numeric-claim vector). Also contradicts the §04g spec without justification. mplstereonet is correct as specified. |
| Defer `coverage_gap` until after the bronze→silver completeness sweep lands properly | Ship only `project_summary` in this wave; gap-analysis after the corpus-health writers are wired. | The strongest gap signal is the `ingest_manifest` (39,744) ↔ `silver.reports` (1,209) join, which is queryable today. Holding the intent doesn't improve the answer — it just hides a useful capability. |
| Six-PR sequence with extractor work first | Build extractors before any UI ships. | Slowest path to first user value. The 3 data-ready cards work today against already-populated silver/gold rows. Sequencing extractors first leaves Kyle with no visible progress for ~3 weeks. |

## Doc edits applied alongside this ADR

### §04j — agentic retrieval intent table

Two new rows added:

| Intent | Retrieval shape | Answer shape |
|---|---|---|
| `project_summary` | SQL aggregate over `silver.campaigns` + `silver.collars` + `silver.geophysics_surveys` + `silver.reports`, joined to `silver.projects` for scope. Optional vector secondary for narrative context. | OIUR template with a `breakdown_table` payload + optional `timeline_payload` card. |
| `coverage_gap` | `bronze.ingest_manifest` ↔ `silver.reports` join for ingest-stage gaps; per-dimension coverage queries against silver tables for extraction-stage gaps. Vector secondary for narrative. | OIUR template with a `coverage_table` payload + optional `map_payload` card showing spatial holes. |

### §04g — visualization inventory

Three rows already exist (Drillhole strip log, Cross-section, Stereonet). Add explicit "chat-card" column denoting which payload kind serves each:

| Visualization | Chat-card payload | Card-only or full-view? |
|---|---|---|
| Drillhole strip log | `drillhole_payload` | Both. Card is a compact strip from `gold.drillhole_intervals_visual`; full view at `/drillholes/{id}`. |
| Cross-section panels | (future) `cross_section_payload` | Full view today, card kind reserved. |
| Stereonet | `stereonet_payload` | Card via mplstereonet base64 PNG + point list. |
| Map view | `map_payload` (GeoJSON, already shipped) | Card today, formalized in this ADR. |
| (new) Timeline | `timeline_payload` | Card. Horizontal swimlanes by technique × year. |
| (existing) 3D drillhole | `viz_payload.chart_type='drill_trace_3d'` | Card via Plotly 3D scatter — already shipped (DrillTrace3D.tsx). |

## Sequenced PRs

| PR | Scope | Owner | Blocked by | Approx |
|---|---|---|---|---|
| **PR-1** | `project_summary` + `coverage_gap` intents; map / timeline / drillhole cards; envelope payload types | backend-fastapi + frontend-engineer | this ADR | 1 week |
| **PR-2** | `silver_structure_populate` 1st-stage extractor; stereonet card (mplstereonet server render) | data-engineer + frontend-engineer | this ADR | 1 week |
| **PR-3** | Contractor / geologist / lab NER backfill across silver.reports; QP nodes pushed to Neo4j per §04f | data-engineer | this ADR | 3 days |
| **PR-4** | `silver_drill_traces` activation; optional enhancement of existing Plotly 3D card (DrillTrace3D.tsx) with downhole-curve overlay | data-engineer + frontend-engineer | PR-2 (for structure overlay) | 2-3 days (shrunk — card already exists) |

## Verification

- `intent_classifier.py` `INTENT_LABELS` tuple grows to 8 entries.
- `state.py` `ContextEnvelope` carries the new typed payload kinds.
- `silver_structure_populate` Dagster asset materializes; `silver.structure` row count > 0 in dev.
- Stereonet card payload includes both base64 PNG and the underlying point list (`source_row_ids` per §04i).
- `project_summary` queries return populated breakdown rows for `drill_type`, `drill_date`, `company` immediately (post-PR-1); for `contractor`, `geologist`, `lab_name` post-PR-3.
- `coverage_gap` queries return ingest-stage gap rows (manifest vs reports) post-PR-1; extraction-stage gap rows (e.g. "67% of collars have no alteration log") post-PR-3.
- PR-4 ships no new frontend dependencies — existing Plotly stack handles the 3D card.

## SME questions deferred to PRs

- **PR-2**: Equal-area (Schmidt) or equal-angle (Wulff) default for stereonet?
- **PR-3**: For QPs lifted from `silver.reports.authors`, treat the report's `effective_date` as the QP's tenure marker?
