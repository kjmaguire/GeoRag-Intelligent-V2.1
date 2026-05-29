# Master-plan §5 (Spatial pipeline + drillhole visuals) — Scope Proposal

**Document version:** 1.0 (proposal — not yet a kickoff)
**Audience:** Kyle (decide whether/when to open §5 as the next master-plan phase)
**Authored:** doc-phase 69, overnight autonomous run 2026-05-13

**Status of §3:** Steps 1-8 complete (§3 functionally done except for
Step 9 corpus pass + Step 10 RAGFlow retirement — both blocked on SME
labeling time). §3 is the longest master-plan phase by tick count
(doc-phases 49-66 = 18 ticks); §5 should be comparably substantial.

---

## What §5 ships, in plain language

"Show drillholes on a map and as cross-sections."

Concretely, master plan §5 deliverables (verbatim):
1. GeoPandas/Rasterio/Shapely fully integrated into FastAPI ingestion paths
2. Minimum-curvature desurvey producing `silver.drill_traces` cleanly
3. `gold.drillhole_intervals_visual`, `gold.cross_section_panels`,
   `gold.structure_measurements_visual` materialized via Dagster
4. First three visualizations: **strip logs, cross-sections, stereonets**
   (Plotly interactive + matplotlib static)
5. Chart export contract enforced (§17.4)
6. Drillhole Visual QA Agent + Visual Readiness Agent

**Done test:** a drillhole array can be visualized as strip logs +
cross-sections with provenance + chart export contract metadata;
the Visual Readiness Agent correctly explains when a visualization
is/isn't possible.

---

## What already exists (v1.49 baseline checked 2026-05-13)

Existing silver tables relevant to §5:
- `silver.collars` — drillhole collar locations (1 row populated for
  test fixture; see fixture state in `project_phase18_31_autonomous_run.md`
  memory entry for the 20-collar test corpus)
- `silver.drill_traces` — pre-computed minimum-curvature traces.
  Has a `silver_drill_traces` Dagster asset (doc-phase 1 era).
  LINESTRINGZ in EPSG:4326. `survey_hash` enables idempotent recompute.
- `silver.surveys` — survey measurements feeding the desurvey
- `silver.seismic_surveys` — separate, not §5 scope

So the **minimum-curvature desurvey is already done** (doc-phase 1 era).
That's deliverable #2 satisfied.

What's missing per the master plan:
- `gold.drillhole_intervals_visual` — table doesn't exist
- `gold.cross_section_panels` — table doesn't exist
- `gold.structure_measurements_visual` — table doesn't exist
- Strip log visualization
- Cross-section visualization
- Stereonet visualization
- Chart export contract (§17.4)
- Drillhole Visual QA Agent
- Visual Readiness Agent

---

## Recommended sub-step breakdown

Mirror §3's autonomous-loop cadence: one tick per logical sub-step.

| Sub-step | What | Estimated ticks |
|---|---|---|
| 5.1 | Verify v1.49 GeoPandas/Rasterio/Shapely integration; gap-fill any missing pyproject deps. Small. | 1 |
| 5.2 | Audit `silver.drill_traces` against test fixture; confirm minimum-curvature produces clean output for the 20-collar corpus. May surface tuning needs. | 1 |
| 5.3 | `gold.drillhole_intervals_visual` migration + Dagster asset. Joins silver.collars + silver.surveys + silver.lithology_intervals + silver.assays. Window functions for visual depth ranges. | 1-2 |
| 5.4 | `gold.cross_section_panels` migration + Dagster asset. Pre-projects collars onto a section line per project_id. | 1 |
| 5.5 | `gold.structure_measurements_visual` migration + Dagster asset. Joins silver.structures + collar locations + computes plot coordinates. | 1 |
| 5.6 | Strip log visualization — Plotly + matplotlib generator. New FastAPI endpoint `GET /internal/v1/viz/strip_log?collar_id=&format=` returning either interactive HTML/JSON or static PNG. | 1-2 |
| 5.7 | Cross-section visualization — similar shape; consumes `gold.cross_section_panels`. | 1-2 |
| 5.8 | Stereonet visualization — fold/foliation orientations; `mplstereonet` Python lib. | 1 |
| 5.9 | Chart export contract enforcement (§17.4 — read the spec, implement metadata embedding). | 1 |
| 5.10 | Drillhole Visual QA Agent — Pydantic AI agent that audits visual output for sanity (e.g., does the stripe log have lithology bars covering the full depth range?). | 1-2 |
| 5.11 | Visual Readiness Agent — predicts whether a visualization is possible given what's in silver (e.g., "no surveys for this collar — strip log only, no cross-section"). | 1-2 |
| 5.12 | React Inertia frontend pages — Drillhole Detail page with embedded strip log + cross-section views. May span 2-3 ticks for the actual operator UI. | 2-4 |
| 5.13 | Acceptance test — full sweep against the 20-collar test corpus. | 1 |

**Total: 14-22 doc-phase ticks.** §3 was 18 ticks. Similar magnitude.

---

## Dependencies + risks

### Hard dependencies

- `silver.collars` + `silver.surveys` + `silver.lithology_intervals` +
  `silver.assays` schemas must be stable (they are, from v1.49)
- PostGIS extension (already enabled per doc-phase 0)
- `mplstereonet` Python lib — NOT currently a project dep; would need
  to add to `pyproject.toml`

### Soft dependencies / friction

- **Chart export contract §17.4** — I haven't read this section in
  depth. The "contract" likely means a JSON sidecar with provenance +
  what-tools-can-import-it metadata. Worth a 30-min read before 5.9
  to make sure the visualizations emit the right shape.
- **Pydantic AI agent patterns (5.10, 5.11)** — there are existing
  agents in `app/agents/phase0/` to model after. Stable pattern.
- **Frontend rendering of interactive Plotly inside Inertia React**
  — straightforward but worth a small spike to confirm the
  rendering approach (CDN vs npm package vs server-side static HTML).

### Risks

- **Visualization quality is subjective.** §5's done-test is "the
  Visual Readiness Agent correctly explains when a visualization is
  or isn't possible." That's a behavior check, not a pixel-perfect
  check. Easier than I initially worried about.
- **Drillhole desurvey edge cases** — the existing
  `silver.drill_traces` asset has known edge cases (single-survey
  vertical, high-dogleg). The 20-collar test corpus may not exercise
  all of them. Expect some test-data expansion mid-§5.
- **Frontend complexity creep** — the Drillhole Detail page could
  balloon into "Drillhole Database UI." Bound the v1 scope: just
  the strip log + cross-section + stereonet on a single-collar page;
  no multi-collar comparison, no editing.

---

## Open questions for Kyle to decide at 8am

1. **Should §5 open before Step 9 (corpus) lands?** §5 doesn't
   technically depend on §3 acceptance. Could parallelize: autonomous
   §5 work + your SME labeling time in parallel. Risk: divides
   attention between two big workstreams.
2. **Plotly version (Inertia React component vs HTML embed)?** —
   product decision. Plotly's React component (`react-plotly.js`)
   is more flexible but adds ~1MB to the JS bundle. HTML embed via
   `<iframe srcDoc>` is simpler but limits interactivity.
3. **`mplstereonet` dependency OK?** — MIT-licensed, actively
   maintained, well-aligned with the project's free-licensing rule.
   Reasonable add per `feedback_free_licensing.md` memory.
4. **§5.10 / §5.11 agent priorities** — are these "must ship" for
   §5 done, or "ship later"? Master plan says yes-ship; my read is
   they're behavioral correctness for the chat experience. Confirm.

---

## What I would do autonomously NEXT if green-lit

(If you say "open §5" without further specifics)

**Doc-phase 70** = §5 Step 1 + 2:
- Audit current GeoPandas/Rasterio/Shapely state in
  `src/fastapi/pyproject.toml` + `src/dagster/pyproject.toml`
- Run a sanity check on `silver.drill_traces` against the 20-collar
  fixture — confirm geometries are reasonable + coord_origin matches
  EPSG:4326
- If both pass: write the §5 kickoff doc (`docs/phase5_master_plan_kickoff.md`)
  with the 14-sub-step breakdown, then start opening 5.3 (gold tables)
  in the following tick

If something fails the audit, halt + document for your review.

---

## What I would NOT do autonomously

- Add `mplstereonet` to pyproject.toml without your sign-off
- Open §6 (PublicGeo + MapLibre layer packs) — much larger frontend
  surface; product decisions needed
- Touch §17.4 chart export contract without reading it first
- Run any §5 sub-step that requires real geological judgment
  (e.g. "is this dogleg reasonable?")

---

## TL;DR

§5 is a coherent ~18-tick workstream. The minimum-curvature desurvey
is already done (v1.49). The gold visual tables + 3 visualizations +
2 agents are the bulk of the work. Frontend integration adds 2-4
ticks. Risks are manageable; biggest unknown is the chart export
contract from §17.4 which I haven't read yet.

If you want §5 to be the next phase, doc-phase 70 can open with
Step 5.1+5.2 (audit + sanity) and produce a proper kickoff doc that
mirrors `docs/phase3_master_plan_kickoff.md` shape.

If you'd rather Step 9 happen first, the path is the SME labeling
work + writing `scripts/phase3_master_plan_acceptance.sh` (~150
lines of Python — could be doc-phase 70 work).

Either choice is sensible. Both work blocks are well-supported by
existing infrastructure.
