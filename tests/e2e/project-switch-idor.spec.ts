import { expect, test, type APIRequestContext, type PlaywrightWorkerArgs } from '@playwright/test';

/**
 * Cross-tenant IDOR (Module 9 9.4): one signed-in tenant must not be able to
 * read or use another tenant's project through the HTTP API.
 *
 * What this spec used to be, and why it is not any more
 * -----------------------------------------------------
 * It POSTed `/api/queries` and GETted `/api/answer-runs/<id>/citations`; neither
 * route exists (the API lives under `/api/v1`), and it accepted 403, 404 AND 422,
 * so a route that did not exist passed. Its "foreign" project was a UUID that
 * exists nowhere, so it could not tell "denied" from "not found". And its
 * `beforeEach` waited for a URL (`/dashboard|chat|portfolio`) that Login.tsx never
 * navigates to (it visits `/projects`), so every test timed out before the first
 * assertion. It could not fail for the reason it existed.
 *
 * What it asserts now
 * -------------------
 * Two REAL tenants, each signed in through the SPA's own cookie flow
 * (GET /sanctum/csrf-cookie, then POST /api/v1/auth/spa-login with the XSRF
 * header: the sequence in resources/js/Pages/Login.tsx). For each direction
 * (A looking at B, B looking at A):
 *
 *   - CONTROL: the viewer reads ITS OWN project and gets 200 with that project.
 *     Without it, a 404 below could just mean the route is broken or the
 *     environment is not seeded, and the spec would pass for the wrong reason.
 *   - the other tenant's project is refused with EXACTLY the status the API
 *     documents: 404 on the project, coverage-density and collars routes
 *     (existence-oracle defence), 403 on POST /api/v1/queries (see
 *     QueryController::store);
 *   - nothing of the other tenant's project comes back in the denial body;
 *   - the project list never contains the other tenant's project;
 *   - a denied read, and a denied query, are indistinguishable from the same
 *     request against a project that does not exist.
 *
 * Environment (the spec FAILS, not skips, when it is incomplete)
 * --------------------------------------------------------------
 *   E2E_BASE_URL                 default http://localhost:8888
 *   E2E_USER_EMAIL / _PASSWORD   tenant A  (default demo@georag.dev / password)
 *   E2E_PROJECT_ID               a project tenant A is a member of
 *   E2E_OTHER_USER_EMAIL / _PASSWORD
 *                                tenant B: a user in a DIFFERENT workspace
 *   E2E_OTHER_PROJECT_ID         a project tenant B is a member of and A is not
 * The project ids come from whatever seeded the two tenants, NOT from the API
 * under test: deriving "mine" from GET /api/v1/projects would let a broken list
 * define its own ground truth. The Laravel app must treat BASE_URL as a Sanctum
 * stateful domain (SANCTUM_STATEFUL_DOMAINS includes its host:port). No browser
 * is needed: the checks go over HTTP with a cookie jar, the way the SPA's
 * fetch() does.
 *
 * The login limiter allows 5 attempts a minute per email and IP, and this spec
 * signs each tenant in once per run (in beforeAll), not once per test.
 */

const BASE_URL = process.env.E2E_BASE_URL ?? 'http://localhost:8888';

interface Credentials {
    email: string;
    password: string;
}

interface Tenant {
    label: string;
    api: APIRequestContext;
    /** A project this tenant is a member of: ground truth from the seeder, not from the API. */
    projectId: string;
}

const NEVER_A_PROJECT = '00000000-0000-4000-8000-00000000dead';

function requireEnv(name: string): string {
    const value = process.env[name];
    expect(
        value,
        `${name} is not set. This spec needs a second tenant (a user in another workspace) and will not pass without one.`,
    ).toBeTruthy();
    return value as string;
}

async function xsrfHeader(api: APIRequestContext): Promise<Record<string, string>> {
    const cookies = (await api.storageState()).cookies;
    const token = cookies.find((cookie) => cookie.name === 'XSRF-TOKEN')?.value;
    return token ? { 'X-XSRF-TOKEN': decodeURIComponent(token) } : {};
}

/** The SPA's login sequence (resources/js/Pages/Login.tsx), over an API context with a cookie jar. */
async function signIn(
    playwright: PlaywrightWorkerArgs['playwright'],
    label: string,
    credentials: Credentials,
    projectId: string,
): Promise<Tenant> {
    const api = await playwright.request.newContext({
        baseURL: BASE_URL,
        extraHTTPHeaders: {
            Accept: 'application/json',
            // Sanctum starts a session only for "stateful" requests, i.e. ones whose
            // Origin/Referer host is in SANCTUM_STATEFUL_DOMAINS. A browser always sends Origin.
            Origin: BASE_URL,
            Referer: `${BASE_URL}/`,
        },
    });

    const csrf = await api.get('/sanctum/csrf-cookie');
    expect(csrf.status(), `${label}: GET /sanctum/csrf-cookie`).toBeLessThan(300);

    const login = await api.post('/api/v1/auth/spa-login', {
        data: { email: credentials.email, password: credentials.password },
        headers: await xsrfHeader(api),
    });
    expect(login.status(), `${label}: POST /api/v1/auth/spa-login as ${credentials.email}`).toBe(200);

    return { label, api, projectId };
}

async function listedProjectIds(tenant: Tenant): Promise<string[]> {
    const response = await tenant.api.get('/api/v1/projects?per_page=100');
    expect(response.status(), `${tenant.label}: GET /api/v1/projects`).toBe(200);
    return ((await response.json()) as { data: Array<{ project_id: string }> }).data.map((p) => p.project_id);
}

let tenantA: Tenant;
let tenantB: Tenant;

test.describe.configure({ mode: 'serial' });

test.beforeAll(async ({ playwright }) => {
    tenantA = await signIn(
        playwright,
        'tenant A',
        {
            email: process.env.E2E_USER_EMAIL ?? 'demo@georag.dev',
            password: process.env.E2E_USER_PASSWORD ?? 'password',
        },
        requireEnv('E2E_PROJECT_ID'),
    );
    tenantB = await signIn(
        playwright,
        'tenant B',
        {
            email: requireEnv('E2E_OTHER_USER_EMAIL'),
            password: requireEnv('E2E_OTHER_USER_PASSWORD'),
        },
        requireEnv('E2E_OTHER_PROJECT_ID'),
    );
    expect(tenantA.projectId, 'the two tenants must be tested with two different projects').not.toBe(tenantB.projectId);
});

test.afterAll(async () => {
    await Promise.all([tenantA?.api.dispose(), tenantB?.api.dispose()]);
});

const DIRECTIONS: Array<[string, () => Tenant, () => Tenant]> = [
    ['tenant A looking at tenant B', () => tenantA, () => tenantB],
    ['tenant B looking at tenant A', () => tenantB, () => tenantA],
];

for (const [title, viewerOf, otherOf] of DIRECTIONS) {
    test.describe(`IDOR: ${title}`, () => {
        test('control: the viewer reads its own project (200)', async () => {
            const viewer = viewerOf();
            const own = viewer.projectId;

            const response = await viewer.api.get(`/api/v1/projects/${own}`);

            expect(response.status()).toBe(200);
            expect(((await response.json()) as { data: { project_id: string } }).data.project_id).toBe(own);
        });

        test('control: the viewer can reserve a query on its own project (202)', async () => {
            const viewer = viewerOf();
            const own = viewer.projectId;

            const response = await viewer.api.post('/api/v1/queries', {
                data: { query: 'How many drill holes are in this project?', project_id: own },
                headers: await xsrfHeader(viewer.api),
            });

            expect(response.status()).toBe(202);
            expect(((await response.json()) as { query_id?: string }).query_id).toBeTruthy();
        });

        test("the other tenant's project is exactly 404, and the body reveals nothing about it", async () => {
            const viewer = viewerOf();
            const foreign = otherOf().projectId;

            const response = await viewer.api.get(`/api/v1/projects/${foreign}`);

            expect(response.status()).toBe(404);
            const text = await response.text();
            expect(text).not.toContain(foreign);
            expect(text).not.toMatch(/"project_name"|"collar_count"|"company"/);
        });

        test("the other tenant's coverage-density and collars are exactly 404", async () => {
            const viewer = viewerOf();
            const foreign = otherOf().projectId;

            for (const path of [
                `/api/v1/projects/${foreign}/coverage-density`,
                `/api/v1/projects/${foreign}/collars`,
            ]) {
                const response = await viewer.api.get(path);
                expect(response.status(), path).toBe(404);
            }
        });

        test("a query on the other tenant's project is exactly 403 and reserves nothing", async () => {
            const viewer = viewerOf();
            const foreign = otherOf().projectId;

            const response = await viewer.api.post('/api/v1/queries', {
                data: { query: 'How many drill holes are in this project?', project_id: foreign },
                headers: await xsrfHeader(viewer.api),
            });

            expect(response.status()).toBe(403);
            const body = (await response.json()) as Record<string, unknown>;
            expect(body.error).toBe('forbidden');
            expect(body).not.toHaveProperty('query_id');
            expect(body).not.toHaveProperty('channel');
        });

        test("control: the viewer's project list contains its own project", async () => {
            const viewer = viewerOf();

            expect(await listedProjectIds(viewer)).toContain(viewer.projectId);
        });

        test("the viewer's project list never contains the other tenant's project", async () => {
            const viewer = viewerOf();
            const foreign = otherOf().projectId;

            expect(await listedProjectIds(viewer)).not.toContain(foreign);
        });

        test('a denied read is indistinguishable from a read of a project that does not exist', async () => {
            const viewer = viewerOf();
            const foreign = otherOf().projectId;

            const denied = await viewer.api.get(`/api/v1/projects/${foreign}`);
            const absent = await viewer.api.get(`/api/v1/projects/${NEVER_A_PROJECT}`);

            expect(denied.status()).toBe(absent.status());
            expect(((await denied.json()) as { message?: string }).message).toBe(
                ((await absent.json()) as { message?: string }).message,
            );
        });

        test('a denied query is indistinguishable from a query on a project that does not exist', async () => {
            const viewer = viewerOf();
            const foreign = otherOf().projectId;
            const ask = async (projectId: string) =>
                viewer.api.post('/api/v1/queries', {
                    data: { query: 'How many drill holes are in this project?', project_id: projectId },
                    headers: await xsrfHeader(viewer.api),
                });

            const denied = await ask(foreign);
            const absent = await ask(NEVER_A_PROJECT);

            expect(denied.status()).toBe(absent.status());
            expect(await denied.text()).toBe(await absent.text());
        });
    });
}
