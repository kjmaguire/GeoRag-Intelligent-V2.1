import { test, expect } from '@playwright/test';

/**
 * Cross-project IDOR safety check (Module 9 9.4).
 *
 * Direct-fetches `/internal/queries` with a project_id the logged-in
 * user does NOT have membership in. The server must respond 403 (or
 * 404 — either is acceptable as long as no project data leaks).
 *
 * jsdom can issue fetch but the surrounding Inertia/Sanctum cookie
 * dance is fragile under it. Real-browser only.
 */

const BASE_URL = process.env.E2E_BASE_URL ?? 'http://localhost:8888';
const TEST_EMAIL = process.env.E2E_USER_EMAIL ?? 'demo@georag.dev';
const TEST_PASSWORD = process.env.E2E_USER_PASSWORD ?? 'password';

// A UUID that is well-formed but does not belong to the demo workspace.
// Per Module 9 testing convention: zeros except for a deterministic
// suffix so the server can't accidentally accept it as the demo project.
const FOREIGN_PROJECT_ID = '00000000-0000-0000-0000-0000deadbeef';

test.describe('IDOR: foreign project_id is rejected', () => {
    test.beforeEach(async ({ page }) => {
        await page.goto(`${BASE_URL}/login`);
        await page.getByLabel(/email/i).fill(TEST_EMAIL);
        await page.getByLabel(/password/i).fill(TEST_PASSWORD);
        await page.getByRole('button', { name: /log in|sign in/i }).click();
        await page.waitForURL(/\/(dashboard|chat|portfolio)/, { timeout: 10_000 });
    });

    test('querying a project the user does not belong to is rejected', async ({ page }) => {
        const response = await page.evaluate(async ({ url, body }) => {
            const r = await fetch(url, {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                },
                body: JSON.stringify(body),
            });
            return { status: r.status, text: await r.text() };
        }, {
            url: `${BASE_URL}/api/queries`,
            body: { query: 'How many drill holes?', project_id: FOREIGN_PROJECT_ID },
        });

        // Either 403 (auth-style refusal) or 404 (presence-hiding refusal)
        // is acceptable. 200 is a leak — flag it.
        expect([403, 404, 422]).toContain(response.status);
        // Belt-and-suspenders: response body must NOT contain the foreign UUID
        // echoed back as a successful answer. (Refusal echoes are fine.)
        expect(response.text).not.toMatch(/"answer_run_id"\s*:\s*"[0-9a-f-]{36}"/i);
    });

    test('citation download for a foreign answer_run_id is rejected', async ({ page }) => {
        const response = await page.evaluate(async (url) => {
            const r = await fetch(url, {
                credentials: 'include',
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
            });
            return { status: r.status };
        }, `${BASE_URL}/api/answer-runs/${FOREIGN_PROJECT_ID}/citations`);

        expect([403, 404]).toContain(response.status);
    });
});
