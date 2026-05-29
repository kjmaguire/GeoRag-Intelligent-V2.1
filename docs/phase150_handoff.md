## Doc-phase 150 handoff — §6.3 NRCan Canadian Mines adapter live

**Status:** Live + 6/6 pytest cases + 12 NRCan mines in DB. **94/94 substrate verifier**.

## What landed

Second §6 adapter graduation following the doc-phase 149 pattern.
Lands canonical mine rows in `public_geoscience.pg_mine` under
`source_id='nrcan_canadian_mines'`.

### New service — `app/services/publicgeo/nrcan_mines_adapter.py`

Same shape as `bc_minfile_adapter.py`:
- `sync_nrcan_canadian_mines(pool=None)` — orchestration
- `_fetch_nrcan_mines_features()` — synthetic stub (12 mines)
- `_feature_checksum()` — idempotency helper
- `NRCanMinesSyncResult` NamedTuple

### 12 synthetic seeds — federal coverage

Major Canadian mines across 6 provinces/territories + 7 commodity
groupings:

| Mine | Province | Commodity grouping | Operator |
|---|---|---|---|
| Diavik Diamond Mine | NT | gemstones | Rio Tinto |
| Eskay Creek (restart) | BC | precious_metals | Skeena Resources |
| Detour Lake | ON | precious_metals | Agnico Eagle |
| Voisey's Bay | NL | base_metals | Vale |
| Highland Valley Copper | BC | base_metals | Teck Resources |
| Cigar Lake | SK | uranium | Cameco |
| McArthur River | SK | uranium | Cameco |
| Raglan | QC | base_metals | Glencore |
| Sullivan (closed) | BC | base_metals | Teck |
| Nechalacho REE | NT | ree | Vital Metals |
| Lanigan Potash | SK | potash_salt | Nutrien |
| Elk Valley Coal | BC | coal | Glencore |

The federal map view (CA-FEDERAL jurisdiction) now has 12 visible
points across the country plus the 15 BC MINFILE points from
doc-phase 149.

## Tests — `src/fastapi/tests/test_nrcan_mines_adapter.py`

**6 pytest cases, all green:**

| Test | Verifies |
|---|---|
| `test_fetch_features_returns_12_mines` | 12 features, all in Canada bbox |
| `test_synthetic_mines_span_multiple_provinces` | ≥5 provinces covered |
| `test_synthetic_mines_cover_commodity_groups` | All 7 expected commodity groupings present |
| `test_sync_nrcan_mines_inserts_all_12` | Clean state → 12 inserts |
| `test_sync_nrcan_mines_idempotent` | Re-run → 0 inserts, 0 updates |
| `test_sync_nrcan_mines_emits_audit` | Audit anchor lands |

## Live verification

```text
Total NRCan mines: 12
commodity_grouping:
  base_metals      4
  uranium          2
  precious_metals  2
  gemstones        1
  ree              1
  potash_salt      1
  coal             1
```

Add the §6.2 BC MINFILE (15 occurrences) and §6.3 NRCan Mines (12)
totals: **27 public-geoscience features now on the map across the
two adapters**.

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest tests/test_nrcan_mines_adapter.py -v
# → 6 passed in 0.58s

bash scripts/autonomous_run_substrate_verify.sh
# → 94/94 checks passed
```

## Cumulative session state

- **Doc-phase ticks this run:** 150
- **§6 PublicGeo adapters graduated:** **2 of 9**
  (bc_minfile_mineral_occurrence, nrcan_canadian_mines)
- **Hatchet workflow skeletons graduated:** 6 of 11
- **§25.4 support agents graduated:** 5 of 5
- **§18.2 nodes graduated:** 6 of 12
- **§15.1 nodes graduated:** 4 of 12
- **§21.3 capture hooks wired:** 1 of 8
- **Live pytest cases:** 185 (179 + 6)
- **Substrate verifier:** **94/94 PASS**

## Remaining §6 adapters (7 of 9)

| Source | Jurisdiction | Target table |
|---|---|---|
| `sk_mineral_occurrence` | CA-SK | pg_mineral_occurrence |
| `sk_drillhole_collar` | CA-SK | pg_drillhole_collar |
| `sk_assessment_survey` | CA-SK | pg_assessment_survey |
| `bc_aris_assessment_survey` | CA-BC | pg_assessment_survey |
| `bc_minfile_drillhole_collar` | CA-BC | pg_drillhole_collar |
| `ab_ags_bedrock_geology` | CA-AB | pg_bedrock_geology |
| `nrcan_geo_bedrock_geology` | CA-FED | pg_bedrock_geology |

Each clones the established pattern. Roughly one tick per adapter.

## What's next

- **Doc-phase 151** — `sk_mineral_occurrence` adapter (Saskatchewan
  mineral occurrences; primary Saskatchewan launch jurisdiction)
- **Doc-phase 152+** — keep ticking adapters; or pivot to another
  scope per Kyle's direction

## Carry-overs

- Same as doc-phase 149: synthetic stubs land 12-15 representative
  rows. Real fetchers from NRCan Atlas / GEO.ca swap in via
  `_fetch_*` function replacement.
- `commodity_grouping='coal'` is supported by the CHECK constraint —
  good catch for Elk Valley.
