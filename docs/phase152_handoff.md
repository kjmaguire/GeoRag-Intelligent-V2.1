## Doc-phase 152 handoff — §6.1 SK drillhole collar adapter live

**Status:** Live + 6/6 pytest cases + 12 SK drillholes in DB. **96/96 substrate verifier**.

## What landed

Fourth §6 adapter. First **drillhole-collar** adapter (different
target table than the prior 3). Lands rows in
`public_geoscience.pg_drillhole_collar` under
`source_id='sk_drillhole_collar'`.

### Schema mapping additions vs mineral-occurrence adapters

| Drillhole column | Source field |
|---|---|
| `drillhole_id` | drillhole_id |
| `drillhole_name` | drillhole_name |
| `company` | company |
| `project_name` | project_name |
| `date_drilled` | date_drilled (ISO date string → Python date) |
| `drill_type` | drill_type (diamond_core / rotary / etc.) |
| `commodity_of_interest` | text[] of commodity symbols |
| `total_length_m`, `inclination_deg`, `azimuth_deg`, `collar_elevation_m` | NUMERIC drilling parameters |
| `core_availability` | CHECK-constrained (available / partial / unavailable / unknown) |
| `stratigraphic_depths` | jsonb (default '{}') |

### 12 seeded SK drillholes

| Project | Commodity | Count |
|---|---|---|
| McArthur River, Cigar Lake, PLS, Arrow, Wheeler River | U (Athabasca) | 5 |
| Seabee, Flin Flon, Jolu, La Ronge Belt | Au + base metals | 4 |
| Muskowekwan, Jansen Project | K (potash) | 2 |
| Hoidas Lake | REE | 1 |

Real drillhole IDs (McArthur River MR-101, NexGen AR23-189, BHP Jansen
KH-3, etc.), real operators (Cameco, NexGen, Hudbay, BHP), realistic
drilling parameters (lengths 155-1825m, inclinations -45 to -90°).

## Tests — 6/6 pytest cases green

- Fetcher: 12 features in SK bbox; lengths reasonable; multiple drill_types
- DB writes: 12 inserts + drillhole-specific columns populated correctly
- Idempotency + audit anchor

## Cumulative state

- **Doc-phase ticks this run:** 152
- **§6 PublicGeo adapters graduated:** **4 of 9**
- **PublicGeo rows in DB:** **53 total** (15 BC occ + 12 NRCan mines +
  14 SK occ + 12 SK drillholes)
- **Live pytest cases:** 197 (191 + 6)
- **Substrate verifier:** **96/96 PASS**

## Remaining §6 adapters (5 of 9)

| Source | Target table |
|---|---|
| `sk_assessment_survey` | pg_assessment_survey |
| `bc_aris_assessment_survey` | pg_assessment_survey |
| `bc_minfile_drillhole_collar` | pg_drillhole_collar (clone of doc-phase 152) |
| `ab_ags_bedrock_geology` | pg_bedrock_geology |
| `nrcan_geo_bedrock_geology` | pg_bedrock_geology |

## What's next

- **Doc-phase 153** — `bc_minfile_drillhole_collar` adapter (clones
  doc-phase 152 pattern, different jurisdiction)
- **Doc-phase 154+** — continue through remaining adapters
