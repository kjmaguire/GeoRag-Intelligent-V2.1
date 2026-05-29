## Doc-phase 135 handoff — §6 public_geoscience jurisdictions + sources foundation

**Status:** 5 jurisdictions + 9 sources seeded. **78/78 substrate verifier**.

## Scope rationale

The original plan was "§6 BC MINFILE PublicGeo adapter." Reality check:
the `public_geoscience.*` schema existed but **all tables were empty
(0 rows)**. The "Saskatchewan done" claim from the §6 scope proposal
was schema-only, not data — even SK had no jurisdictions metadata or
source registrations.

Real BC MINFILE / NRCan adapter ingestion is a multi-tick effort
per jurisdiction (ArcGIS REST WFS fetches, schema mapping,
license verification, refresh scheduling). The high-leverage
one-tick move is to seed the **foundation reference data** so:
1. The §6 admin surface has something to display
2. Future adapter ticks have target rows to upsert into
3. License/CRS metadata is in one place for export compliance

## What landed

### Migration — `database/migrations/2026_05_13_180000_seed_public_geoscience_jurisdictions_and_sources.php`

Seeds 5 jurisdictions + 9 sources, idempotent via upsert on the
primary keys (`jurisdiction_code`, `source_id`).

#### Jurisdictions (5)

| Code | Name | Status | Sort | Authority |
|---|---|---|---|---|
| CA-FEDERAL | Canada (Federal) | active | 5 | NRCan — Geological Survey of Canada |
| CA-SK | Saskatchewan | active | 10 | Saskatchewan Ministry of Energy & Resources |
| CA-BC | British Columbia | active | 20 | BC Ministry of Energy, Mines and Low Carbon Innovation |
| CA-AB | Alberta | active | 30 | AER + Alberta Geological Survey |
| CA-MB | Manitoba | coming_soon | 40 | Manitoba Geological Survey |

Each row carries: country_code, level, primary_authority,
license_summary + url, default_source_crs (EPSG), refresh_cadence,
teaser.

#### Sources (9)

| source_id | Jurisdiction | Canonical type |
|---|---|---|
| `sk_mineral_occurrence` | CA-SK | mineral_occurrence |
| `sk_drillhole_collar` | CA-SK | drillhole_collar |
| `sk_assessment_survey` | CA-SK | assessment_survey |
| `bc_minfile_mineral_occurrence` | CA-BC | mineral_occurrence |
| `bc_aris_assessment_survey` | CA-BC | assessment_survey |
| `bc_minfile_drillhole_collar` | CA-BC | drillhole_collar |
| `ab_ags_bedrock_geology` | CA-AB | bedrock_geology |
| `nrcan_canadian_mines` | CA-FEDERAL | mine |
| `nrcan_geo_bedrock_geology` | CA-FEDERAL | bedrock_geology |

Each source row has: service_url (the upstream ArcGIS/WFS endpoint),
source_crs, license_summary, refresh_cadence, notes. `last_refreshed_at`
is intentionally NULL so the §6 admin surface can show "never pulled"
state correctly until adapters land.

The 9 sources represent the **target inventory** for §6.2-§6.3
adapter work. Each is a one-tick ingestion landing point.

## Smoke verification

```bash
# Migration applied
php artisan migrate --force
# → 2026_05_13_180000_seed_public_geoscience_jurisdictions_and_sources DONE

# Data verification
SELECT count(*) FROM public_geoscience.jurisdictions;  -- 5
SELECT count(*) FROM public_geoscience.sources;        -- 9

# Pint
vendor/bin/pint --dirty --format agent
# → {"tool":"pint","result":"passed"}

# Substrate verifier (added 2 checks)
bash scripts/autonomous_run_substrate_verify.sh
# → 78/78 checks passed
```

## Cumulative session state

- **Doc-phase ticks this run:** 135
- **§6 foundation seeded:** 5 jurisdictions + 9 sources
- **§6 adapter graduations remaining:** 9 sources (one tick each)
- **Live pytest cases:** 81
- **Substrate verifier:** **78/78 PASS**

## What's deferred to follow-on ticks

Each source row in the registry waits for its own adapter:

| Source | Adapter complexity |
|---|---|
| `sk_mineral_occurrence` | Medium (ArcGIS REST, well-documented) |
| `sk_drillhole_collar` | Medium |
| `sk_assessment_survey` | Medium (polygons + DPA linkage) |
| `bc_minfile_mineral_occurrence` | Medium (similar to SK) |
| `bc_aris_assessment_survey` | Medium |
| `bc_minfile_drillhole_collar` | Medium |
| `ab_ags_bedrock_geology` | Large (1:1M coverage) |
| `nrcan_canadian_mines` | Small (~200 mines) |
| `nrcan_geo_bedrock_geology` | Large (1:5M coverage) |

The smallest adapter (`nrcan_canadian_mines`) is the most leverageable
next pick.

## What's next

Continuing the partial-section closeout sequence:

- **Doc-phase 136** — §10.11 first support agent (ticket_triage) — synthetic
  stub pattern matching doc-phase 132/134. Or alternatively, the
  smallest §6 adapter (`nrcan_canadian_mines`).
- **Doc-phase 137** — §7-A v1 report_builder first graph nodes
- **Doc-phase 138** — §8 score_targets graph nodes + §8.7 formula

## Carry-overs

- The `nrcan_canadian_mines` adapter is the highest-leverage §6
  follow-on: small dataset (~200 mines), federal CRS (3978), well-known
  schema. One tick.
- `public_geoscience.last_service_edit_ms` is a known optimization
  field (daily short-circuit per master plan §05e) that adapters
  should populate to avoid redundant fetches.
- The Manitoba jurisdiction is `status='coming_soon'` since MGS
  doesn't expose a comparable WFS. Source rows aren't seeded for it.
