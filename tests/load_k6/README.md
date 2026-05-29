# GeoRAG — k6 Load Test Harness (§11.9 + §11.9b)

Five scripts that exercise the production-critical surfaces under
realistic load. §11.9b adds the §28 SLO battery (map tiles + report
builder) on top of the §11.9 starter set.

| Script | Surface | §28 SLO |
| --- | --- | --- |
| `rag_query.k6.js` | `/v1/rag/query` end-to-end RAG | 20 RPS, p95 < 5s (tighter than the §28 8s ceiling) |
| `ingestion_upload.k6.js` | `POST /v1/documents` + lifecycle | 5 RPS, p95 < 8s |
| `viz_strip_log.k6.js` | `/v1/viz/strip_log` (§5 renderer) | 30 RPS, p95 < 2s |
| **`map_tile_fetch.k6.js`** | Martin tile sources (mines/drillholes/density) | **p95 < 200ms @ 100 vu** |
| **`report_build.k6.js`** | §7 report planner + section editor PUT | **p95 plan < 30s; p95 draft < 2s** |

## Full battery

`./run_section11_battery.sh` runs every script sequentially, captures
results to `docs/load_tests/section11_battery_<ts>.md`, exits 0 only
if every script meets its SLO.

```bash
./tests/load_k6/run_section11_battery.sh           # full battery
./tests/load_k6/run_section11_battery.sh quick     # short stages
```

## Running locally

```bash
# Set the auth + base URL first
export GEORAG_BASE_URL="http://localhost:8000"
export GEORAG_BEARER_TOKEN="<sanctum-pat>"
export GEORAG_WORKSPACE_ID="11111111-..."

# Then any of:
docker run --rm -i --network host \
  -e GEORAG_BASE_URL -e GEORAG_BEARER_TOKEN -e GEORAG_WORKSPACE_ID \
  -v "$PWD/tests/load_k6:/scripts" \
  grafana/k6 run /scripts/rag_query.k6.js
```

## Stages

Each script defines a 4-stage profile:

1. **warmup** — 30s ramp-up to 25% target
2. **steady** — 2 min at target RPS
3. **peak** — 30s burst to 150% target
4. **cool-down** — 30s ramp-down

Thresholds:

- `http_req_failed { rate<0.01 }` — error rate below 1%
- `http_req_duration { p(95) < <SLO ms> }`
- `checks { rate>0.99 }` — assertion pass-rate

## Output

k6 writes a JSON summary to `tests/load_k6/results/<script>-<ts>.json`
when invoked with `--summary-export=`. The CI run uploads these as
artifacts so regressions are obvious in PRs.

## Hooking into CI

GitHub Actions job `load-smoke` runs each script with
`--vus 5 --duration 30s` (smoke profile) on every PR. Full load runs
happen nightly via the `nightly-load-test` workflow against the staging
cluster.
