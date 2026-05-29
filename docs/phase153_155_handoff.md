## Doc-phases 153 → 155 handoff — §6 PublicGeo adapters CLOSED (9 of 9)

**Status:** Live + 16/16 pytest cases + 95 PublicGeo rows on the map. **99/99 substrate verifier**.

**§6 PublicGeo is now the second section fully closed in this run** (after §25.4 support agents at doc-phase 144).

## What landed across these 3 ticks

### Doc-phase 153 — `bc_minfile_drillhole_collar` adapter

10 BC drillholes across BC mining districts (Highland Valley, Brucejack,
Mount Milligan, Eskay Creek, Kemess East, Endako, Tulsequah, Wedge
Pegmatite, Rossland, Boss Mountain). Realistic operators + drilling
parameters. 4/4 pytest cases green.

### Doc-phase 154 — SK + BC assessment-survey adapters

Both jurisdictions in one file (`assessment_survey_adapters.py`)
because the target table + shape are identical. 16 surveys total
(8 SK SaskGeoAtlas + 8 BC ARIS) with MultiPolygon footprints
(airborne / ground / underground types). 7/7 pytest cases.

The shared `_sync_assessment_surveys()` helper pattern is reusable —
when a new jurisdiction's assessment-file feed comes online, only
the synthetic stub list changes.

### Doc-phase 155 — AB + NRCan bedrock-geology adapters — §6 CLOSED

Same shared-helper pattern as 154. 16 bedrock units total:

**Alberta (8)**: McMurray bitumen sands, Belly River Group, Canadian
Shield NE Alberta exposure, Misty Mountain Fm, Rocky Mountain
Cordilleran thrust belt, Leduc Reef Complex, Paskapoo Fm, Elk Point
evaporites.

**NRCan 1:5M federal coverage (8)**: Canadian Shield, Western Canada
Sedimentary Basin, Cordilleran Orogen, Appalachian Orogen,
Trans-Hudson Orogen, Franklin Orogen Arctic, Athabasca Basin sandstone,
Innuitian Orogen.

5/5 pytest cases.

## Final §6 data state — 95 PublicGeo features on the map

| Table | Rows | Sources |
|---|---|---|
| `pg_mineral_occurrence` | 29 | bc_minfile (15) + sk_mineral (14) |
| `pg_mine` | 12 | nrcan_canadian_mines |
| `pg_drillhole_collar` | 22 | sk_drillhole (12) + bc_minfile_drill (10) |
| `pg_assessment_survey` | 16 | sk_assessment (8) + bc_aris (8) |
| `pg_bedrock_geology` | 16 | ab_ags (8) + nrcan_geo (8) |
| **Total** | **95** | **9 adapters across 4 jurisdictions** |

`audit.audit_ledger` carries **122 `public_geoscience.pull.complete`
anchors** documenting every sync invocation across the run.

## Full §6 adapter set (9 of 9 — CLOSED)

| # | Adapter | Tick | Rows | Jurisdiction |
|---|---|---|---|---|
| 1 | bc_minfile_mineral_occurrence | 149 | 15 | CA-BC |
| 2 | nrcan_canadian_mines | 150 | 12 | CA-FEDERAL |
| 3 | sk_mineral_occurrence | 151 | 14 | CA-SK |
| 4 | sk_drillhole_collar | 152 | 12 | CA-SK |
| 5 | bc_minfile_drillhole_collar | 153 | 10 | CA-BC |
| 6 | sk_assessment_survey | 154 | 8 | CA-SK |
| 7 | bc_aris_assessment_survey | 154 | 8 | CA-BC |
| 8 | ab_ags_bedrock_geology | 155 | 8 | CA-AB |
| 9 | nrcan_geo_bedrock_geology | 155 | 8 | CA-FEDERAL |

All adapters share:
- Synthetic-stub `_fetch_*` function (real ArcGIS REST swaps in
  without changing surrounding code)
- Idempotent UPSERT keyed on `(source_id, source_feature_id)`
- SHA-256 checksum for change detection
- `public_geoscience.pull.complete` audit anchor per sync
- `sources.last_refreshed_at` bump

## Tests added across ticks 153-155: 16 cases, all green

| Module | Cases |
|---|---|
| `test_bc_drillhole_adapter.py` | 4 |
| `test_assessment_survey_adapters.py` | 7 |
| `test_bedrock_geology_adapters.py` | 5 |

## Smoke verification

```bash
bash scripts/autonomous_run_substrate_verify.sh
# → 99/99 checks passed
```

## Cumulative session state — 22 ticks closed

- **Doc-phase ticks this run (since 132):** **22 ticks** (132 → 155)
- **Sections closed:** **§25.4** (support agents) + **§6** (PublicGeo
  adapters)
- **§18.2 nodes graduated:** 6 of 12
- **§15.1 nodes graduated:** 4 of 12
- **§21.3 capture hooks wired:** 1 of 8
- **Hatchet workflow skeletons graduated:** 6 of 11
- **Reasoning agent skeletons graduated:** 1
- **LangGraph wirings live:** 2 of 2
- **Live pytest cases:** **213** (up from 66 at start of run)
- **Substrate verifier:** **99/99 PASS**

## What's next

Two sections fully closed. The remaining partial-section work is
material:

- **§15.1 nodes** — 8 of 12 still skeleton (LLM-dependent — needs
  vLLM integration)
- **§18.2 nodes** — 6 of 12 still skeleton (retrieval + spatial +
  LLM + SeaweedFS dependent)
- **§21.3 capture hooks** — 7 of 8 still need their upstream
  human-UI flows to ship
- **Hatchet workflow bodies** — 5 of 11 remaining are data/infra
  dependent

Options:
- Wire `what_changed_detector` (147) into the §7.2 what_changed
  report template — closes a tick-level integration
- Real LLM evaluator for §10.4 (replaces `synthetic_stub` in
  workspace_evaluator)
- §21.3 capture hooks at `crs_decision` or `schema_mapping` sites
  in the ingest pipeline

## Carry-overs

- Real BC MINFILE has ~12k+ rows, NRCan Atlas has ~600 mines.
  Synthetic stubs land 8-15 each — when real fetchers swap in,
  paging support is needed.
- `commodity_grouping` CHECK constraints covered all groupings we
  needed (precious_metals, base_metals, uranium, lithium, ree,
  potash_salt, coal, gemstones, industrial_materials).
- Permissions needed for each new public_geoscience table — pattern
  is GRANT SELECT, INSERT, UPDATE, DELETE for georag_app + same on
  *_history tables. Future adapter source tables follow the same
  pattern.
