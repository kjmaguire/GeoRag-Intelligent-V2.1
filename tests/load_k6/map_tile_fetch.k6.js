// §11.9b k6 load script — MapLibre tile fetches via Martin.
//
// §28 SLO: p95 tile fetch < 200ms, error rate < 1%, no 5xx under 100 vu.
//
// Mimics MapLibre's tile-request pattern: random panning across the
// Saskatchewan basin (-110 to -101 lon, 53 to 59 lat) at zoom levels
// 5-12, hitting the pg_mines_fn + pg_drillhole_collars_fn function
// tile sources via the Laravel proxy.
//
// Run:
//   docker run --rm -i --network host \
//     -e GEORAG_BASE_URL -e GEORAG_BEARER_TOKEN \
//     -v "$PWD/tests/load_k6:/scripts" \
//     grafana/k6 run /scripts/map_tile_fetch.k6.js

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Rate } from 'k6/metrics';

const BASE_URL = __ENV.GEORAG_BASE_URL || 'http://localhost:8000';
// Direct Martin endpoint (no Laravel proxy) when running against dev stack —
// production benchmarks should hit the proxy URL via __ENV.GEORAG_BASE_URL.
const TILE_BASE = __ENV.GEORAG_TILE_BASE || `${BASE_URL}/tiles`;
const TOKEN = __ENV.GEORAG_BEARER_TOKEN || '';

// Tile sources to exercise. Each is registered in docker/martin/martin.yaml.
const LAYERS = [
  'pg_mines_fn',
  'pg_drillhole_collars_fn',
  'pg_mineral_occurrences_fn',
  'density_choropleth_h3',
];

// SK basin bounding box — viewports likely to return non-empty tiles.
const LON_MIN = -110, LON_MAX = -101;
const LAT_MIN = 53,   LAT_MAX = 59;

// MapLibre / TMS tile coordinates: at zoom z the world is 2^z tiles wide.
// Convert (lon, lat, z) to (x, y).
function lonLatToTile(lon, lat, z) {
  const n = 2 ** z;
  const x = Math.floor((lon + 180) / 360 * n);
  const latRad = lat * Math.PI / 180;
  const y = Math.floor((1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2 * n);
  return [x, y];
}

function randomTile() {
  const z = 5 + Math.floor(Math.random() * 8);  // zoom 5..12
  const lon = LON_MIN + Math.random() * (LON_MAX - LON_MIN);
  const lat = LAT_MIN + Math.random() * (LAT_MAX - LAT_MIN);
  const [x, y] = lonLatToTile(lon, lat, z);
  const layer = LAYERS[Math.floor(Math.random() * LAYERS.length)];
  return { layer, z, x, y };
}

export const options = {
  scenarios: {
    tile_fetch: {
      executor: 'ramping-arrival-rate',
      startRate: 10,
      timeUnit: '1s',
      preAllocatedVUs: 100,
      maxVUs: 300,
      stages: [
        { duration: '30s', target: 20 },   // warmup
        { duration: '1m',  target: 100 },  // §28 target
        { duration: '30s', target: 200 },  // 2x peak
        { duration: '30s', target: 0 },    // cool-down
      ],
    },
  },
  thresholds: {
    // §28 SLO targets per master_plan_section11_kickoff.md locked defaults
    'http_req_failed':                       ['rate<0.01'],
    'http_req_duration{stage:steady}':       ['p(95)<200'],   // 200ms tile p95
    'http_reqs{layer:density_choropleth_h3}': ['count>1'],     // exercised at least once
    'checks':                                ['rate>0.99'],
  },
};

const tileBytes = new Trend('tile_bytes');
const emptyRate = new Rate('tile_empty_rate');

export default function () {
  const t = randomTile();
  const url = `${TILE_BASE}/${t.layer}/${t.z}/${t.x}/${t.y}`;
  const res = http.get(url, {
    headers: { 'Authorization': TOKEN ? `Bearer ${TOKEN}` : '' },
    tags: { stage: 'steady', layer: t.layer, zoom: String(t.z) },
  });

  check(res, {
    'status 200 or 204':       (r) => r.status === 200 || r.status === 204,
    'not a 5xx':               (r) => r.status < 500,
    'content-type is protobuf': (r) =>
      r.status === 204 ||
      (r.headers['Content-Type'] || '').includes('protobuf'),
  });

  if (res.status === 200 && res.body) {
    tileBytes.add(res.body.length);
    emptyRate.add(res.body.length === 0 ? 1 : 0);
  }

  sleep(0.02);  // MapLibre fires bursts; small pacing jitter
}
