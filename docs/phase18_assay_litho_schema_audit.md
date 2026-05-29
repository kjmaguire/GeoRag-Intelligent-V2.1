# Phase 18 Step 1 — Assay + lithology schema audit

**Document version:** 1.0
**Status:** Snapshot at Phase 18 open.

Captures the table shapes Phase 18 Step 2 needs to seed, plus
the workspace/FK plumbing that has to be in place first.

---

## 1. silver.samples

| Column | Type | Notes |
|--------|------|-------|
| sample_id | uuid | PK |
| collar_id | uuid | FK → silver.collars |
| from_depth | double | NOT NULL; > 0 |
| to_depth | double | NOT NULL; > from_depth |
| sample_type | varchar(20) | one of: Core / Chip / Grab / Channel / Soil (per `parsers/csv_sample.py` `VALID_SAMPLE_TYPES`) |
| lab_id | varchar(50) | nullable |
| commodity_assays | jsonb | keys like `U3O8_ppm`, `Au_ppb`, `Cu_pct` (regex per parser) |
| qaqc_type | varchar(20) | nullable |
| commodity_assay_flags | jsonb | nullable; parser-set quality flags |
| workspace_id | uuid | **NOT NULL** — FK → workspaces.workspace_id |

The workspace_id constraint is the gotcha. Phase 13's `silver.projects`
seed set `workspace_id = NULL`, but `silver.samples.workspace_id`
can't be NULL. Phase 18 sets the project's workspace_id to the
default workspace (`a0000000-0000-0000-0000-000000000001`) so the
samples can share it.

RLS policy: `samples_project_scope` filters by
`current_setting('georag.project_id', true)`. Direct SQL inserts
during fixture seeding bypass this (RLS is enforced on the role
in app context, not the bootstrap role); no special handling
needed in the migration.

---

## 2. silver.lithology_logs

| Column | Type | Notes |
|--------|------|-------|
| log_id | uuid | PK |
| collar_id | uuid | FK → silver.collars |
| from_depth | double | NOT NULL; ≥ 0 |
| to_depth | double | NOT NULL; > from_depth |
| lithology_code | varchar(20) | nullable; free-form code |
| lithology_description | text | nullable |
| grain_size | varchar(20) | nullable |
| color | varchar(50) | nullable |
| hardness | varchar(20) | nullable |
| rqd | double | nullable; in [0, 100] |
| recovery | double | nullable; in [0, 100] |
| weathering | varchar(20) | nullable |

**No workspace_id** on `lithology_logs`. Cleaner than samples;
just the collar FK + depth-interval invariants.

---

## 3. Commodity assays JSON shape

The parser (`csv_sample.py` line 54) accepts keys matching:
```
^(U3O8|Au|Ag|Cu|Pb|Zn|Ni|Fe|Ti|Li)_?(ppm|pct|ppb|pct_|_pct)?$
```

So `U3O8_ppm`, `Au_ppb`, `Cu_pct` etc. are all valid. The agent's
`query_assay_data` tool likely reads any of these formats.

For gq-014 (expects "U3O8" + "52" in response), seeding samples
with `U3O8_ppm` values peaking at 52,000 should produce a response
mentioning "U3O8" and "52,000" (or "52000" or "5.2%").

For gq-017 (expects "Au"), at least one sample needs an `Au_*` key.
We'll add `Au_ppb` to two samples.

---

## 4. Lithology codes for gq-015

Test expects "SST" and "PGN" in the response. Common geological
codes:
- SST = Sandstone (in Athabasca Basin: Athabasca Sandstone, above unconformity)
- PGN = Paragneiss / Pegmatite (below unconformity in basement rocks)
- OVB = Overburden (surface till + soil)
- GNT = Granite

Phase 18 seeds PLS-20-01 with a realistic Athabasca-Basin-style
log:
- 0-50m: OVB (overburden / till)
- 50-200m: SST (Athabasca Sandstone)
- 200-300m: PGN (basement paragneiss)
- 300-320m: GNT (granitic intrusion)

---

End of audit.
