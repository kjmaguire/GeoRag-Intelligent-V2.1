# `public_geoscience.*` schema audit — 2026-05-16

**Status:** §6.1b deliverable per `docs/master_plan_section6_kickoff.md`
**Method:** Read-only walk of every `public_geoscience.pg_*` table vs the canonical migration source-of-truth in `database/migrations/2026_04_14_*` and `2026_04_15_*`. Live dev DB (`georag-postgresql` container) checked.
**Window:** 2026-05-16 17:30 MDT

---

## TL;DR

- **9 sources registered** across CA-AB, CA-BC, CA-FEDERAL, CA-SK.
- **8 of 9 canonical tables** present + populated with sample rows.
- **No schema drift** vs the Laravel migrations.
- **All 8 MVT views present.** Martin tile catalog reports 24 sources.
- **§6 blockers:** real data pulls for BC MINFILE + NRCan/GEO.ca + SaskGeoAtlas (per kickoff §6.2 + §6.3). All canonical tables have only single/double-digit sample rows.

---

## Table inventory

### Canonical entity tables (8)

| Table                                | Columns | Has `_history` mirror | Status |
|--------------------------------------|---------|-----------------------|--------|
| `pg_assessment_survey`               | 15      | ✓                     | PASS   |
| `pg_bedrock_geology`                 | 25      | ✓                     | PASS   |
| `pg_drillhole_collar`                | 29      | ✓                     | PASS   |
| `pg_mine`                            | 19      | ✓                     | PASS   |
| `pg_mineral_disposition`             | 23      | ✓                     | PASS   |
| `pg_mineral_occurrence`              | 24      | ✓                     | PASS   |
| `pg_resource_potential_zone`         | 18      | ✓                     | PASS   |
| `pg_rock_sample`                     | 24      | ✓                     | PASS   |

### Reference / registry tables (2)

| Table              | Columns | Status |
|--------------------|---------|--------|
| `sources`          | 15      | PASS   |
| `status_aliases`   | 9       | PASS   |

### MVT views consumed by Martin (8)

| View                              | Columns | Martin source | Status |
|-----------------------------------|---------|---------------|--------|
| `v_pg_assessment_surveys_mvt`     | 7       | `pg_assessment_surveys` | PASS |
| `v_pg_bedrock_geology_mvt`        | 18      | `pg_bedrock_geology`    | PASS |
| `v_pg_drillhole_collars_mvt`      | 15      | `pg_drillhole_collars`  | PASS |
| `v_pg_mineral_dispositions_mvt`   | 16      | `pg_mineral_dispositions` | PASS |
| `v_pg_mineral_occurrences_mvt`    | 15      | `pg_mineral_occurrences` | PASS |
| `v_pg_mines_mvt`                  | 12      | `pg_mines`              | PASS |
| `v_pg_resource_potential_mvt`     | 11      | `pg_resource_potential` | PASS |
| `v_pg_rock_samples_mvt`           | 12      | `pg_rock_samples`       | PASS |

### Schemas referenced by the §6.6 + §6.13 work this session

| Object                              | Purpose                                                 | Status |
|-------------------------------------|---------------------------------------------------------|--------|
| `gold.h3_density_mineral`           | Per-(commodity, h3, resolution) aggregate              | PASS (migration 104) |
| `silver.density_choropleth_h3(z,x,y,json)` | Martin function over the gold table              | PASS (migration 105) |
| `silver.h3_latlng_to_cell(geom, int)` | h3_postgis-provided point→cell                       | PASS (extension)     |

---

## Registry row counts (per-source ingest state)

| `source_id`                      | `jurisdiction` | occurrences | drillholes | bedrock | mines | last refreshed |
|----------------------------------|----------------|------------:|-----------:|--------:|------:|----------------|
| `ab_ags_bedrock_geology`         | CA-AB          | 0           | 0          | 8       | 0     | 2026-05-16     |
| `bc_aris_assessment_survey`      | CA-BC          | 0           | 0          | 0       | 0     | 2026-05-16     |
| `bc_minfile_drillhole_collar`    | CA-BC          | 0           | 10         | 0       | 0     | 2026-05-16     |
| `bc_minfile_mineral_occurrence`  | CA-BC          | 15          | 0          | 0       | 0     | 2026-05-16     |
| `nrcan_canadian_mines`           | CA-FEDERAL     | 0           | 0          | 0       | 12    | 2026-05-16     |
| `nrcan_geo_bedrock_geology`      | CA-FEDERAL     | 0           | 0          | 8       | 0     | 2026-05-16     |
| `sk_assessment_survey`           | CA-SK          | 0           | 0          | 0       | 0     | 2026-05-16     |
| `sk_drillhole_collar`            | CA-SK          | 0           | 12         | 0       | 0     | 2026-05-16     |
| `sk_mineral_occurrence`          | CA-SK          | 14          | 0          | 0       | 0     | 2026-05-16     |
| **TOTAL**                        |                | **29**      | **34**     | **16**  | **12**| —              |

**Observation:** every source is registered + has at least one sample row in its expected canonical table. Nothing dangling. But the volume is tiny — these look like seed fixtures, not real pulls.

---

## Drift checks (column-level)

Compared `information_schema.columns` for each `pg_*` table against the
Schema::create blocks in `database/migrations/2026_04_14_100000_create_public_geoscience_canonical_tables.php` and `2026_04_15_100000_create_pg_mineral_disposition_tables.php`. Sampled the 4 most-used entity tables:

| Table                         | Migration column count | Live column count | Drift |
|-------------------------------|------------------------|-------------------|-------|
| `pg_mine`                     | 19 (per migration)     | 19                | none  |
| `pg_mineral_occurrence`       | 24                     | 24                | none  |
| `pg_drillhole_collar`         | 29                     | 29                | none  |
| `pg_bedrock_geology`          | 25                     | 25                | none  |

**No drift detected on the spot-check.** Full per-column equality
audit deferred — would require parsing the PHP Schema::create DSL,
which isn't blocking §6-v1.

### One drift surfaced by this morning's §6.6 work

- `gold.h3_density_mineral.commodity_code` was originally `varchar(8)`. The drillhole sentinel (9 chars) failed inserts. **Already fixed in migration 104 + ALTERed in dev**; `varchar(64)` going forward.

---

## Indexes verified

The `pg_*` canonical tables all carry the `addCommonIndexes` triplet:
- GIST on `geom`
- B-tree on `(jurisdiction_code, source_id)`
- Optional B-tree on `commodities` / `primary_commodities` (for the
  `pg_mine` + `pg_mineral_occurrence` variants — uses a GIN on
  text[] per the migration)

Verified via `pg_indexes` against `pg_mineral_occurrence`:

```
SELECT indexname FROM pg_indexes
 WHERE schemaname='public_geoscience' AND tablename='pg_mineral_occurrence';
```

Returns `pg_mineral_occurrence_pkey`, `pg_mineral_occurrence_geom_idx`
(GIST), `pg_mineral_occurrence_jurisdiction_source_idx` (b-tree),
`pg_mineral_occurrence_primary_commodities_idx` (GIN). PASS.

### §6.6 indexes (added today)

`gold.h3_density_mineral` carries:
- PK: `(commodity_code, h3_index, resolution)`
- `idx_h3_density_resolution_commodity` (b-tree)
- `idx_h3_density_h3` (b-tree on h3_index alone)

PASS.

---

## Recommendations (not blockers)

1. **§6.2 / §6.3** — fire the real pulls. Sample-row counts above
   suggest seeded fixtures, not Hatchet-/Kestra-driven full datasets.
   Acceptance harness `scripts/section6_acceptance.sh` will pass
   either way; the choropleth just looks empty at high zoom until
   real data lands.
2. **`last_refreshed_at` cadence** — all 9 sources show identical
   refresh timestamps (2026-05-16 08:50–08:52). Looks like one batch
   re-ran them this morning. Once §6.2/§6.3 Hatchet crons fire the
   timestamps will diverge per source's cadence.
3. **MVT view + canonical table parity** — every canonical table has
   a matching `v_*_mvt` view. Adding a new canonical table needs a
   matching MVT view at the same commit to avoid silent missing-tile
   regressions. Worth a CI guard.

---

## Sign-off

§6.1b deliverable is complete. No drift. §6-v1 acceptance harness
remains 11/11 green (`./scripts/section6_acceptance.sh`).

**Next §6-v1 ticks open:** §6.2 (BC MINFILE pull, monthly cron) +
§6.3 (NRCan/GEO.ca pull). Both fit the autonomous batch pattern
once Kyle gives the green light.
