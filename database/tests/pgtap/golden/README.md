# Golden MVT Snapshot Directory

This directory holds reference artifacts for `10_golden_mvt_snapshots.sql`.

## Contents

| File | Purpose |
|------|---------|
| `manifest.json` | Captured etag_hash, mvt_md5, and byte lengths for all 7 silver MVT layers |
| `generate.sh` | Script that re-captures hashes from the running database |
| `*.mvt` | Binary MVT tile files (best-effort; captured by generate.sh, not committed) |

## When to regen

Regen is required (and ONLY acceptable) when:

1. A silver MVT function body changes deliberately (column list, simplification tolerance, etc.)
2. The GoldenFixture seed data changes (`seed_golden_fixture.sql`)
3. PostgreSQL or PostGIS major version upgrade changes the MVT encoding format

Do NOT regen because of unrelated schema changes that don't affect the 7 silver
function implementations.

## Regen procedure

```bash
# 1. Ensure the container is running with latest migrations applied
docker compose up -d postgresql
php artisan migrate --force

# 2. Ensure the seed fixture is loaded (idempotent)
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
PGTAP_DIR="database/tests/pgtap"
SCRIPT_DIR="${PGTAP_DIR}"
SQL_FILE="${SCRIPT_DIR}/seed_golden_fixture.sql"
docker cp "${SQL_FILE}" georag-postgresql:/tmp/pgtap_seed.sql
docker exec georag-postgresql psql -U georag -d georag -f /tmp/pgtap_seed.sql

# 3. Run the generator
bash database/tests/pgtap/golden/generate.sh

# 4. Review manifest.json — confirm all 7 layers have non-zero mvt_bytes
#    and that the md5 values changed in a way that makes sense for what changed.

# 5. Update the baked-in hashes in 10_golden_mvt_snapshots.sql
#    (7 hashes in BLOCK 2; BLOCK 3 determinism tests self-update automatically)

# 6. Run the full suite to confirm
bash database/tests/pgtap/run.sh

# 7. Commit manifest.json + 10_golden_mvt_snapshots.sql together
#    Include a regen justification in the commit message, e.g.:
#    test(pgtap): regen golden MVT snapshots — pg_collars_by_project added elevation property
```

## Fixture specification

- Project: `00000000-0000-0000-0000-deadbeefcafe` (`GoldenFixture`)
- Workspace: `a0000000-0000-0000-0000-000000000001` (Default Workspace)
- data_version: 1
- Tile tested: z=3, x=1, y=2 (covers lon -135 to -90, lat ~41 to ~67 WGS84)
- Fixture center: lon≈-110, lat≈55 (UTM zone 13N, EPSG:32613)

The fixture covers 7 source tables:

| Layer | Source table | Row count | Geometry |
|-------|-------------|-----------|----------|
| collars | `silver.collars` | 3 | Point, EPSG:32613 |
| drill_traces | `silver.drill_traces` | 3 | LineStringZ, EPSG:4326 |
| seismic | `silver.seismic_surveys` | 1 | Polygon bbox, EPSG:4326 |
| boundaries | `silver.project_boundaries` | 1 | MultiPolygon, EPSG:4326 |
| formations | `silver.geological_formations` | 1 | MultiPolygon, EPSG:4326 |
| historic_workings | `silver.historic_workings` | 2 | Point, EPSG:4326 |
| geochem | `silver.geochemistry` | 3 | Point, EPSG:4326 |

## ETag note

All 7 etag_hash values in the manifest are identical (`5e649996...`). This is
correct: they share the formula `md5(data_version|z|x|y|project_id)`. The
snapshot test asserts on `md5(mvt)` which differs per layer.
