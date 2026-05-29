import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright configuration for real-browser E2E tests.
 *
 * Separate from the Vitest suite (jsdom-based, lives under
 * resources/js/**/__tests__) because these tests need a real layout /
 * stacking context / tile worker pipeline.
 *
 * ONE-TIME SETUP
 *   npm install -D @playwright/test
 *   npx playwright install chromium
 *
 * RUN
 *   npx playwright test                 # headless
 *   npx playwright test --headed        # visible browser
 *   npx playwright test --debug         # step through
 *
 * CI INTEGRATION (GitHub Actions, GitLab, etc.)
 *   - name: Install Playwright browsers
 *     run: npx playwright install --with-deps chromium
 *   - name: Run E2E
 *     run: npx playwright test
 *     env:
 *       E2E_BASE_URL: http://localhost:8888
 *       E2E_USER_EMAIL: ci-demo@georag.dev
 *       E2E_USER_PASSWORD: ${{ secrets.CI_DEMO_PASSWORD }}
 */
export default defineConfig({
    testDir: './tests/e2e',
    fullyParallel: false, // map stack shares one postgres + Martin
    forbidOnly: !!process.env.CI,
    retries: process.env.CI ? 2 : 0,
    workers: process.env.CI ? 1 : 1,
    reporter: process.env.CI ? 'github' : 'list',
    timeout: 60_000,

    use: {
        baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:8888',
        trace: 'on-first-retry',
        screenshot: 'only-on-failure',
        video: 'retain-on-failure',
    },

    projects: [
        {
            name: 'chromium',
            use: { ...devices['Desktop Chrome'] },
        },
    ],
});
