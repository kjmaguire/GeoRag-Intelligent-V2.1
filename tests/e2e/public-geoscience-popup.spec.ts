import { test, expect } from '@playwright/test';

/**
 * Real-browser E2E for the Public Geoscience click → popup flow.
 *
 * WHY THIS TEST EXISTS — the popup-rendering bug fixed in mid-April 2026
 * passed every Vitest/React-Testing-Library suite we had, because the
 * root cause was a React portal's ancestor stacking context clipping the
 * card. That bug only reproduces in a real browser with real layout
 * flow. React Testing Library uses jsdom, which doesn't compute stacking
 * contexts; a jsdom-only test gave false green while a real user saw a
 * blank map on every click.
 *
 * These Playwright tests run against the live Laravel + Martin + PostGIS
 * stack at http://localhost:8888 and assert the end-user invariants.
 *
 * PREREQUISITES (one-time)
 *   npm install -D @playwright/test
 *   npx playwright install chromium
 *   Create a test user with demo project membership (see beforeAll below)
 *
 * RUN
 *   npx playwright test
 *   npx playwright test --headed           # watch it in a real window
 *   npx playwright test --debug            # step through with inspector
 *
 * WIRE INTO CI
 *   After `composer install` + `npm ci` + `php artisan migrate --seed`:
 *     npx playwright install --with-deps chromium
 *     npx playwright test
 */

const BASE_URL = process.env.E2E_BASE_URL ?? 'http://localhost:8888';
const TEST_EMAIL = process.env.E2E_USER_EMAIL ?? 'demo@georag.dev';
const TEST_PASSWORD = process.env.E2E_USER_PASSWORD ?? 'password';

test.describe('Public Geoscience map: click → popup', () => {
    test.beforeEach(async ({ page }) => {
        await page.goto(`${BASE_URL}/login`);
        await page.getByLabel(/email/i).fill(TEST_EMAIL);
        await page.getByLabel(/password/i).fill(TEST_PASSWORD);
        await page.getByRole('button', { name: /log in|sign in/i }).click();
        await page.waitForURL(/\/(dashboard|portfolio|public-geoscience)/, { timeout: 10_000 });
    });

    test('clicking a drillhole collar shows a popup with the ID and company', async ({ page }) => {
        await page.goto(`${BASE_URL}/public-geoscience`);

        // Wait for the map canvas to exist and Tier 1 layers to finish
        // installing. The data-testid is set when installMvtLayers completes.
        await page.waitForSelector('canvas.maplibregl-canvas', { timeout: 15_000 });
        await page.waitForTimeout(2000); // let tiles populate

        // Zoom into central SK where drillholes are dense.
        // Jurisdiction auto-selects on load → bbox fly-to completes → we
        // are at ~zoom 6 over the province. Zoom in 4 more levels.
        const canvas = page.locator('canvas.maplibregl-canvas');
        await canvas.click({ position: { x: 400, y: 300 } });
        for (let i = 0; i < 4; i++) {
            await page.keyboard.press('Equal'); // '+' zoom
            await page.waitForTimeout(400);
        }

        // At this zoom, drillhole circles are rendered. Click the canvas
        // center and expect a popup to appear. If no collar sits under the
        // click, MapLibre returns zero features and no popup opens — so we
        // probe a small grid of points until one hits.
        let popupAppeared = false;
        for (const [dx, dy] of [[0, 0], [50, 0], [-50, 0], [0, 50], [0, -50], [100, 100]]) {
            const box = await canvas.boundingBox();
            if (!box) throw new Error('canvas has no box');
            await canvas.click({
                position: { x: box.width / 2 + dx, y: box.height / 2 + dy },
            });
            const popup = page.locator('[data-pg-popup="true"]');
            if (await popup.isVisible({ timeout: 500 }).catch(() => false)) {
                popupAppeared = true;
                break;
            }
        }

        expect(popupAppeared, 'popup should appear for at least one click on the canvas').toBeTruthy();

        // Popup must be in document.body (portal), not nested inside the
        // map container — regression guard for the stacking-context bug.
        const popup = page.locator('[data-pg-popup="true"]');
        const parent = await popup.evaluate(
            (el) => el.parentElement?.tagName.toLowerCase() ?? '',
        );
        expect(parent).toBe('body');

        // Popup must have a close button.
        await expect(popup.getByRole('button', { name: /close feature details/i })).toBeVisible();
    });

    test('popup does not leak null-numeric 0 sentinels to the user', async ({ page }) => {
        // Regression guard for the drillhole `total_length_m` bug: when
        // has_total_length=false, the popup must NOT show "Depth: 0 m".
        //
        // Implementation note: this test doesn't force a specific drillhole
        // under the click. It asserts the general invariant — if a Depth
        // line appears at all, the value is not literally "0 m" (which
        // would mean the MVT COALESCE sentinel leaked through).
        await page.goto(`${BASE_URL}/public-geoscience`);
        await page.waitForSelector('canvas.maplibregl-canvas', { timeout: 15_000 });
        await page.waitForTimeout(3000);

        const canvas = page.locator('canvas.maplibregl-canvas');
        const box = await canvas.boundingBox();
        if (!box) throw new Error('canvas has no box');

        // Scan a 5x5 grid; when a popup opens, assert invariant then move on.
        let depthScanned = 0;
        for (let x = 0.2; x <= 0.8 && depthScanned < 8; x += 0.15) {
            for (let y = 0.2; y <= 0.8 && depthScanned < 8; y += 0.15) {
                await canvas.click({ position: { x: box.width * x, y: box.height * y } });
                const popup = page.locator('[data-pg-popup="true"]');
                if (await popup.isVisible({ timeout: 400 }).catch(() => false)) {
                    depthScanned += 1;
                    const depthLine = popup.locator('text=/Depth/');
                    if (await depthLine.count() > 0) {
                        const depthValue = await depthLine.locator('..').textContent() ?? '';
                        expect(depthValue).not.toMatch(/\b0\s*m\b/);
                    }
                    await popup.getByRole('button', { name: /close/i }).click();
                }
            }
        }
    });
});
