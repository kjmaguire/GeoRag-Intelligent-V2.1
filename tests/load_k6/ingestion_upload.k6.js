// §11.9 k6 load script — POST /v1/documents (ingestion upload)
//
// Steady-state target: 5 RPS, p95 < 8s, error rate < 1%.
// Note: ingestion is async — this exercises the intake handler +
// outbox enqueue; the downstream Hatchet workflow is measured
// separately via Dagster sensor metrics.

import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE_URL     = __ENV.GEORAG_BASE_URL     || 'http://localhost:8000';
const TOKEN        = __ENV.GEORAG_BEARER_TOKEN || '';
const WORKSPACE_ID = __ENV.GEORAG_WORKSPACE_ID || '11111111-1111-1111-1111-111111111111';

// 50KB synthetic PDF stub (header + filler) — large enough to exercise
// the multipart parser without DoSing the test cluster.
const PAYLOAD_BYTES = open('./fixtures/synthetic_doc.txt', 'b');

export const options = {
  scenarios: {
    ingestion: {
      executor: 'ramping-arrival-rate',
      startRate: 1,
      timeUnit: '1s',
      preAllocatedVUs: 20,
      maxVUs: 50,
      stages: [
        { duration: '30s', target: 2 },
        { duration: '2m',  target: 5 },
        { duration: '30s', target: 8 },
        { duration: '30s', target: 0 },
      ],
    },
  },
  thresholds: {
    'http_req_failed':                  ['rate<0.01'],
    'http_req_duration{stage:steady}':  ['p(95)<8000'],
    'checks':                           ['rate>0.99'],
  },
};

export default function () {
  const data = {
    file:           http.file(PAYLOAD_BYTES, `loadtest-${__VU}-${__ITER}.txt`, 'text/plain'),
    workspace_id:   WORKSPACE_ID,
    project_id:     '22222222-2222-2222-2222-222222222222',
    title:          `k6 load test ${__VU}/${__ITER}`,
    classification: 'public',
  };
  const res = http.post(`${BASE_URL}/v1/documents`, data, {
    headers: {
      'Authorization': TOKEN ? `Bearer ${TOKEN}` : '',
    },
    tags: { stage: 'steady' },
  });

  check(res, {
    'status is 202 or 201':       (r) => r.status === 202 || r.status === 201,
    'body returns document_id':   (r) => r.json('document_id') !== undefined,
  });

  sleep(0.2);
}
