## Doc-phase 151 handoff — §6.1 SK mineral occurrence adapter live

**Status:** Live + 6/6 pytest cases + 14 SK occurrences in DB. **95/95 substrate verifier**.

## What landed

Third §6 adapter — Saskatchewan, the primary GeoRAG launch
jurisdiction. Lands canonical mineral-occurrence rows in
`public_geoscience.pg_mineral_occurrence` under
`source_id='sk_mineral_occurrence'`.

### 14 seeded SK occurrences across 3 mineral provinces

**Athabasca Basin — uranium (6)**: McArthur River, Cigar Lake, Key Lake,
Triple R, Arrow, Wheeler River.

**Trans-Hudson Orogen — gold + base metals (4)**: Seabee, Flin Flon (SK
side), Star Lake, Jolu.

**Southern potash belt + REE (4)**: Lanigan, Rocanville, Cory, Hoidas
Lake REE prospect.

External IDs use SMDI numbering (Saskatchewan Mineral Deposit Index)
matching the real SaskGeoAtlas schema.

### Same pattern as doc-phase 149 / 150

- Synthetic-stub fetcher swappable for real SaskGeoAtlas ArcGIS REST
- Idempotent UPSERT keyed on (source_id, source_feature_id)
- Audit anchor `public_geoscience.pull.complete`
- `sources.last_refreshed_at` bumped

## Tests — 6/6 pytest cases green

| Test | Verifies |
|---|---|
| `test_fetch_features_returns_14_sk_occurrences` | 14 features, all in SK bbox |
| `test_synthetic_data_covers_three_sk_mineral_provinces` | uranium + precious_metals + base_metals + potash_salt all present |
| `test_synthetic_data_uses_smdi_external_ids` | All external_ids start with SMDI_ |
| `test_sync_sk_inserts_all_14` | Clean state → 14 inserts |
| `test_sync_sk_idempotent` | Re-run → 0 ops |
| `test_sync_sk_emits_audit` | Audit anchor lands |

## Cumulative state

- **Doc-phase ticks this run:** 151
- **§6 PublicGeo adapters graduated:** **3 of 9** (BC MINFILE +
  NRCan Mines + SK mineral occurrences)
- **PublicGeo rows in DB:** **41 total** (15 BC + 12 NRCan + 14 SK)
- **Live pytest cases:** 191 (185 + 6)
- **Substrate verifier:** **95/95 PASS**

## Remaining §6 adapters (6 of 9)

| Source | Target table |
|---|---|
| `sk_drillhole_collar` | pg_drillhole_collar |
| `sk_assessment_survey` | pg_assessment_survey |
| `bc_aris_assessment_survey` | pg_assessment_survey |
| `bc_minfile_drillhole_collar` | pg_drillhole_collar |
| `ab_ags_bedrock_geology` | pg_bedrock_geology |
| `nrcan_geo_bedrock_geology` | pg_bedrock_geology |

## What's next

- **Doc-phase 152** — `sk_drillhole_collar` adapter (drillholes are a
  different target table; new schema mapping needed but pattern carries)
- **Doc-phase 153+** — continue through the adapter set
