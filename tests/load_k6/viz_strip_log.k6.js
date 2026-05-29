// §11.9 k6 load script — /v1/viz/strip_log (Phase H4 §5 renderer)
//
// Steady-state target: 30 RPS, p95 < 2s, error rate < 1%.
// Exercises the §5 visualisation router (strip-log / cross-section /
// stereonet) in plotly + matplotlib output modes.

import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE_URL     = __ENV.GEORAG_BASE_URL     || 'http://localhost:8000';
const TOKEN        = __ENV.GEORAG_BEARER_TOKEN || '';
const WORKSPACE_ID = __ENV.GEORAG_WORKSPACE_ID || '11111111-1111-1111-1111-111111111111';

// Caller MUST pre-populate gold.drillhole_intervals_visual for these
// hole_ids via the Dagster asset before the load run. Use any 10 real
// hole_ids from the smoke fixture.
const HOLE_IDS = (__ENV.GEORAG_LOAD_HOLE_IDS || '').split(',').filter(Boolean);

export const options = {
  scenarios: {
    viz_strip_log: {
      executor: 'ramping-arrival-rate',
      startRate: 5,
      timeUnit: '1s',
      preAllocatedVUs: 30,
      maxVUs: 120,
      stages: [
        { duration: '30s', target: 10 },
        { duration: '2m',  target: 30 },
        { duration: '30s', target: 45 },
        { duration: '30s', target: 0 },
      ],
    },
  },
  thresholds: {
    'http_req_failed':                  ['rate<0.01'],
    'http_req_duration{stage:steady}':  ['p(95)<2000'],
    'checks':                           ['rate>0.99'],
  },
};

export default function () {
  if (HOLE_IDS.length === 0) {
    console.error('GEORAG_LOAD_HOLE_IDS not set');
    return;
  }
  const hole_id = HOLE_IDS[Math.floor(Math.random() * HOLE_IDS.length)];
  const fmt = Math.random() < 0.5 ? 'plotly' : 'png';
  const res = http.get(
    `${BASE_URL}/v1/viz/strip_log?workspace_id=${WORKSPACE_ID}&hole_id=${hole_id}&format=${fmt}`,
    {
      headers: {
        'Authorization': TOKEN ? `Bearer ${TOKEN}` : '',
      },
      tags: { stage: 'steady', format: fmt },
    },
  );
  check(res, {
    'status is 200':                 (r) => r.status === 200,
    'plotly body has data':          (r) => fmt !== 'plotly' || !!r.json('data'),
    'png body has PNG magic':        (r) => fmt !== 'png' ||
                                            (r.body && r.body.length > 8 &&
                                             r.body[0] === 0x89 && r.body[1] === 0x50),
  });
  sleep(0.03);
}
