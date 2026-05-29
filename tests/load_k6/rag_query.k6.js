// §11.9 k6 load script — /v1/rag/query
//
// Steady-state target: 20 RPS, p95 < 5s, error rate < 1%.
//
// Run:
//   docker run --rm -i \
//     -e GEORAG_BASE_URL -e GEORAG_BEARER_TOKEN -e GEORAG_WORKSPACE_ID \
//     -v "$PWD/tests/load_k6:/scripts" \
//     grafana/k6 run /scripts/rag_query.k6.js

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Rate } from 'k6/metrics';

const BASE_URL     = __ENV.GEORAG_BASE_URL     || 'http://localhost:8000';
const TOKEN        = __ENV.GEORAG_BEARER_TOKEN || '';
const WORKSPACE_ID = __ENV.GEORAG_WORKSPACE_ID || '11111111-1111-1111-1111-111111111111';

const QUERIES = [
  'What is the total depth of hole PLS-22-08?',
  'List the alteration types in the Athabasca basin.',
  'Which holes intersected uranium mineralization above 1000 ppm?',
  'Summarize the structural setting near the McArthur River deposit.',
  'How many drillholes were completed in 2024?',
  'What is the average grade of the high-grade zone?',
];

export const options = {
  scenarios: {
    rag_query: {
      executor: 'ramping-arrival-rate',
      startRate: 5,
      timeUnit: '1s',
      preAllocatedVUs: 50,
      maxVUs: 200,
      stages: [
        { duration: '30s', target: 5 },   // warmup
        { duration: '2m',  target: 20 },  // steady
        { duration: '30s', target: 30 },  // peak
        { duration: '30s', target: 0 },   // cool-down
      ],
    },
  },
  thresholds: {
    'http_req_failed':                  ['rate<0.01'],
    'http_req_duration{stage:steady}':  ['p(95)<5000'],
    'checks':                           ['rate>0.99'],
    'rag_citation_count':               ['avg>=1'],
  },
};

const citationCount = new Trend('rag_citation_count');
const validatedRate = new Rate('rag_validated_rate');

export default function () {
  const q = QUERIES[Math.floor(Math.random() * QUERIES.length)];
  const res = http.post(
    `${BASE_URL}/v1/rag/query`,
    JSON.stringify({ query: q, workspace_id: WORKSPACE_ID }),
    {
      headers: {
        'Content-Type':  'application/json',
        'Authorization': TOKEN ? `Bearer ${TOKEN}` : '',
      },
      tags: { stage: 'steady' },
    },
  );

  const ok = check(res, {
    'status is 200':              (r) => r.status === 200,
    'body has answer':            (r) => r.json('answer') !== undefined,
    'body has citations array':   (r) => Array.isArray(r.json('citations')),
  });

  if (ok && res.status === 200) {
    const citations = res.json('citations') || [];
    citationCount.add(citations.length);
    validatedRate.add(citations.every((c) => c.validated === true) ? 1 : 0);
  }

  sleep(0.05);  // pacing jitter
}
