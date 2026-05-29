# US Jurisdiction Expansion — Scoping Note

**Document version:** 1.0
**Audience:** Kyle (decide rollout order + scope of v1 US support)
**Authored:** 2026-05-23 in response to discovery note `georag-cc-02-chatgpt-missed.md` Item 5.
**Status:** Skeleton landed (CC-02 Item 5). Real MRDS feed is the gating work item.

---

## What just landed

| Artifact | Path | What it does |
|---|---|---|
| US jurisdictions seeder | [UnitedStatesJurisdictionsSeeder.php](../database/seeders/PublicGeoscience/UnitedStatesJurisdictionsSeeder.php) | 6 US jurisdiction rows: US-FED (active), plus US-NV/AZ/AK/CO/CA (coming_soon). USGS MRDS source row. |
| MRDS adapter skeleton | [usgs_mrds_adapter.py](../src/fastapi/app/services/publicgeo/usgs_mrds_adapter.py) | Synthetic-stub fetcher with 8 well-known US mines; production-shaped pg_mine UPSERT + audit. Mirrors the NRCan Canadian Mines adapter pattern. |

Together these prove the Saskatchewan-first architecture genuinely extends to non-Canadian jurisdictions with zero schema changes. Confirmed by the existing `jurisdiction_code` first-class column on every `public_geo.*` canonical table.

---

## What's NOT done

The MRDS fetcher is a synthetic stub. The real MRDS dump is several hundred MB of CSV/SQLite with ~190 native columns. Before this adapter can be called "active":

1. **Replace `_fetch_usgs_mrds_features`** with streaming parsing of the MRDS CSV/SQLite dump from https://mrdata.usgs.gov/mrds/.
2. **Field mapping work** — MRDS has ~190 columns; the adapter writes ~10. Some columns map cleanly (mine name, commodities, lat/lon); others (status taxonomy, operator history, production history) need a translation layer.
3. **Coordinate cleanup** — MRDS has historic rows with low-precision (~3 decimal places, ±100 m), inverted lat/lon, and sentinel zeros. The adapter should detect and skip these rather than write garbage to `pg_mine.geom`.
4. **Re-pull cadence** — MRDS is updated irregularly. A monthly Hatchet workflow with content-hash short-circuit (already implemented in the adapter) is the right default.
5. **Fixture-based unit test** — `tests/test_usgs_mrds_adapter.py` against a small fixture dump (a few hundred rows is enough to exercise the field-mapping edge cases).

Each item is bounded — a competent engineer-day per item, except field mapping which is closer to two days.

---

## Rollout order — recommendation

| Priority | Jurisdiction | Why |
|---|---|---|
| 1 | **US-FED via USGS MRDS** | Lowest friction (one feed covers all states). The skeleton already shipped — switch the stub to real data and the federal layer renders nationally. |
| 2 | **US-NV (Nevada Bureau of Mines and Geology)** | Densest active US exploration. NBMG publishes a state mineral occurrence database + active claim layer via ArcGIS REST — adapter pattern matches `bc_minfile_adapter` exactly. |
| 3 | **US-AZ (Arizona Geological Survey)** | Major Cu/Mo/Au porphyry trend. AZGS has a mineral inventory + Resolution Copper relevance for prospectivity workflows. |
| 4 | **US-AK (Alaska DGGS)** | Frontier exploration, large undigitized data piles — matches the Shaun thesis ("biggest non-digitized opportunity"). Alaska data is messier; defer until 1-3 are stable. |
| 5+ | US-CO, US-CA | Smaller commercial pull. Build only if a specific customer requests them. |

---

## Decision asks for Kyle

1. **Approve the rollout order above, or reorder?** The skeleton lands today regardless; the question is which jurisdiction's real adapter gets engineering time next.
2. **Should the real MRDS adapter ship in the next master-plan phase, or in a follow-up?** Bundling it with the §11+ phases keeps focus; pulling it forward unblocks the "US is the bigger market" thesis sooner.
3. **Is a US customer interested today, or is this for product breadth?** A US pilot customer would force the rollout order onto whichever state they're working in. Without one, NV remains the default.

**Default if no answer:** the skeleton is enough for now; MRDS real-feed work waits until either (a) a US pilot customer materialises or (b) a master-plan phase explicitly schedules it.

---

## How to verify the skeleton

```bash
# Run the new seeder
php artisan db:seed --class='Database\Seeders\PublicGeoscience\UnitedStatesJurisdictionsSeeder'

# Then trigger the adapter (Python REPL inside the fastapi container)
docker exec -it georag-fastapi python -c "
import asyncio
from app.services.publicgeo.usgs_mrds_adapter import sync_usgs_mrds
print(asyncio.run(sync_usgs_mrds()))
"

# Confirm rows landed
docker exec georag-postgresql psql -U georag -d georag -c \
  "SELECT source_id, COUNT(*) FROM public_geo.pg_mine GROUP BY source_id ORDER BY 1;"
```

Expected: `usgs_mrds | 8` alongside whatever Canadian sources are already populated.
