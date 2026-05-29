# pgTAP MVT Function Tests

## Quick start

pgTAP must be installed in the `georag` database. The runner handles everything:

```bash
# Run all pgTAP test files
bash database/tests/pgtap/run.sh

# Run a specific group
bash database/tests/pgtap/run.sh --filter 08   # silver functions only
bash database/tests/pgtap/run.sh --filter 09   # PGEO functions only
bash database/tests/pgtap/run.sh --filter 10   # golden snapshots only
```

Composer shortcuts (from project root):

```bash
composer pgtap          # all files
composer pgtap-silver   # --filter 08
composer pgtap-pgeo     # --filter 09
```

The runner copies each file into the `georag-postgresql` container via
`docker cp` and executes it with `psql -t`. pg_prove is NOT required.

## Installing pgTAP (once per container)

```bash
# Inside the running container
docker exec georag-postgresql bash -c "
  apk add --no-cache build-base perl git postgresql18-dev && \
  cd /tmp && \
  curl -fsSL https://github.com/theory/pgtap/archive/refs/tags/v1.3.3.tar.gz | tar xz && \
  cd pgtap-1.3.3 && make && make install
"
docker exec georag-postgresql psql -U georag -d georag \
  -c "CREATE EXTENSION IF NOT EXISTS pgtap;"
```

## Golden snapshot workflow

The `10_golden_mvt_snapshots.sql` file contains baked-in md5 hashes of MVT
tile bytes for all 7 silver functions. These are captured from a deterministic
fixture project (`GoldenFixture`, id `00000000-0000-0000-0000-deadbeefcafe`).

The seed fixture is in `seed_golden_fixture.sql`. It is applied idempotently:

```bash
# Load/refresh seed fixture (safe to re-run)
cd "$(git rev-parse --show-toplevel)"
SCRIPT_DIR="$(cd database/tests/pgtap && pwd)"
docker cp "${SCRIPT_DIR}/seed_golden_fixture.sql" georag-postgresql:/tmp/pgtap_seed.sql
docker exec georag-postgresql psql -U georag -d georag -f /tmp/pgtap_seed.sql
```

To regen golden hashes after an intentional function or fixture change:

```bash
bash database/tests/pgtap/golden/generate.sh
# Review manifest.json, then update hash values in 10_golden_mvt_snapshots.sql
```

Commit `golden/manifest.json` and `10_golden_mvt_snapshots.sql` together with
a brief regen justification in the commit message.

## CI wiring

A `pgtap` job runs in `.github/workflows/ci.yml` after the `laravel` job
(migrations are proven clean first). It installs pgTAP 1.3.3 from source,
applies migrations, and runs all `database/tests/pgtap/*.sql` via psql.

The job requires the GoldenFixture seed to be loaded before file `10` runs.
The CI job applies it in the same step sequence.

## Test files

| File | Purpose | Assertions |
|------|---------|-----------|
| `08_silver_mvt_functions.sql` | 7 silver MVT functions: existence, signature, etag format, etag bump on data_version increment, determinism, martin_readonly grants | 80 |
| `09_public_geoscience_mvt_functions.sql` | 8 PGEO function wrappers: existence, etag md5 format, two-column return, different coords, determinism, martin_readonly grants | 27 |
| `10_golden_mvt_snapshots.sql` | Golden MVT byte snapshots for all 7 silver functions + determinism re-check | 21 |
| `seed_golden_fixture.sql` | Idempotent seed for `10_golden_mvt_snapshots.sql` — NOT a pgTAP file | — |

**Total pgTAP assertions: 128**

## Support files

| File | Purpose |
|------|---------|
| `run.sh` | Bash runner — copies files into container, runs psql, parses TAP output |
| `golden/generate.sh` | Captures fresh MVT hashes from running DB; outputs `golden/manifest.json` |
| `golden/manifest.json` | Machine-readable record of captured golden hashes and byte lengths |

## Notes

- Files `08`, `09`, and `10` run inside `BEGIN ... ROLLBACK` — no test data is
  committed to the database.
- `seed_golden_fixture.sql` DOES commit (no transaction wrapper). It is
  idempotent via `ON CONFLICT DO NOTHING`.
- The GoldenFixture `data_version = 1` is set at INSERT time. The monotonic
  trigger prevents any decrement within the pgTAP tests.
- All 7 silver function etag_hash values are identical for the same tile+project
  because they share the formula `md5(data_version|z|x|y|project_id)`. This is
  correct by design. The MVT byte hashes differ per layer.
- `martin_readonly` EXECUTE grants are verified in files `08` and `09`.
  SELECT grants on source tables are verified separately in Module 8 Chunk 8.3.
