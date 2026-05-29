// §11.9b k6 load script — §7 Report Builder + section editor.
//
// §28 SLO: p95 small-report build < 30s, error rate < 1%.
//
// Two scenarios:
//   1. report_plan — POST /admin/reports/build (synchronous planning,
//      should be fast — p95 < 2s)
//   2. section_draft_put — PUT a section draft (the hot path for the
//      Phase H4 editor; should be sub-second)
//
// The actual §7 export workflow is async (Hatchet) so we don't load-test
// the full generate_report path here — that's a separate batch when
// the §7 backend graduates from skeleton.
//
// Run:
//   docker run --rm -i --network host \
//     -e GEORAG_BASE_URL -e GEORAG_SERVICE_KEY -e GEORAG_WORKSPACE_ID \
//     -e GEORAG_PROJECT_ID \
//     -v "$PWD/tests/load_k6:/scripts" \
//     grafana/k6 run /scripts/report_build.k6.js

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate } from 'k6/metrics';

const BASE_URL     = __ENV.GEORAG_BASE_URL     || 'http://localhost:8000';
const SERVICE_KEY  = __ENV.GEORAG_SERVICE_KEY  || '';
const WORKSPACE_ID = __ENV.GEORAG_WORKSPACE_ID || 'a0000000-0000-0000-0000-000000000001';
const PROJECT_ID   = __ENV.GEORAG_PROJECT_ID   || '22222222-2222-2222-2222-222222222222';

const REPORT_TYPES = [
  'weekly_project_digest',
  'ingestion_quality',
  'what_changed',
];

export const options = {
  scenarios: {
    report_plan: {
      executor: 'ramping-vus',
      startVUs: 1,
      stages: [
        { duration: '20s', target: 2 },   // warmup
        { duration: '1m',  target: 10 },  // steady
        { duration: '30s', target: 25 },  // peak (still moderate — planning
                                          //       is cheap but the cron-side
                                          //       generate_report is the
                                          //       capacity-bound workflow)
        { duration: '20s', target: 0 },
      ],
      exec: 'planScenario',
    },
    section_draft_put: {
      executor: 'ramping-arrival-rate',
      startRate: 5,
      timeUnit: '1s',
      preAllocatedVUs: 30,
      maxVUs: 100,
      stages: [
        { duration: '20s', target: 10 },
        { duration: '1m',  target: 50 },   // section editing burst
        { duration: '20s', target: 0 },
      ],
      exec: 'draftScenario',
      startTime: '0s',
    },
  },
  thresholds: {
    // Master-plan kickoff lock — §28 default for small-report builds
    'http_req_failed':                                   ['rate<0.01'],
    'http_req_duration{op:report_plan}':                 ['p(95)<30000'],
    'http_req_duration{op:section_draft_put}':           ['p(95)<2000'],
    'checks':                                            ['rate>0.99'],
  },
};

const planSuccess = new Rate('report_plan_success');
const draftSuccess = new Rate('section_draft_success');

const headers = {
  'Content-Type':   'application/json',
  'Accept':         'application/json',
  'X-Service-Key':  SERVICE_KEY,
};

export function planScenario() {
  const rt = REPORT_TYPES[Math.floor(Math.random() * REPORT_TYPES.length)];
  const res = http.post(
    `${BASE_URL}/api/v1/admin/reports/build`,
    JSON.stringify({
      report_type:          rt,
      workspace_id:         WORKSPACE_ID,
      project_id:           PROJECT_ID,
      requested_by_user_id: 1,
    }),
    { headers, tags: { op: 'report_plan' } },
  );

  const ok = check(res, {
    'status 200/201':       (r) => r.status === 200 || r.status === 201,
    'returns build_id':     (r) => r.status >= 300 || !!r.json('build_id'),
    'returns sections':     (r) => r.status >= 300 || Array.isArray(r.json('sections')),
  });
  planSuccess.add(ok ? 1 : 0);
  sleep(0.5);
}

// Shared build state so the draft scenario has something to PUT against.
// The first VU plans a build; subsequent VUs reuse the build_id via
// k6's --vu-only-once trick isn't easy here, so we plan per-VU.
let cachedBuild = null;

// Default export — used when k6 is invoked with --vus/--duration
// overrides (e.g. quick-mode smoke). Falls back to the plan scenario
// since it's the cheapest path that still exercises the admin gate.
export default function () {
  planScenario();
}

export function draftScenario() {
  if (cachedBuild === null) {
    const planRes = http.post(
      `${BASE_URL}/api/v1/admin/reports/build`,
      JSON.stringify({
        report_type:          'weekly_project_digest',
        workspace_id:         WORKSPACE_ID,
        project_id:           PROJECT_ID,
        requested_by_user_id: 1,
      }),
      { headers, tags: { op: 'report_plan_for_draft' } },
    );
    if (planRes.status >= 300) {
      // Planning broken — abort this VU iteration
      sleep(1);
      return;
    }
    cachedBuild = {
      build_id:   planRes.json('build_id'),
      section_id: (planRes.json('sections') || [{}])[0].section_id || 'summary',
    };
  }

  const body = `# Load-test draft body\n\n` +
               `Iteration ${__ITER} on VU ${__VU}, timestamp ${Date.now()}.`;
  const res = http.put(
    `${BASE_URL}/api/v1/admin/reports/builds/${cachedBuild.build_id}/sections/${cachedBuild.section_id}`,
    JSON.stringify({ body_markdown: body, updated_by_user_id: 1 }),
    { headers, tags: { op: 'section_draft_put' } },
  );
  const ok = check(res, {
    'draft 200':            (r) => r.status === 200,
    'returns updated_at':   (r) => r.status !== 200 || !!r.json('updated_at'),
  });
  draftSuccess.add(ok ? 1 : 0);
  sleep(0.05);
}
