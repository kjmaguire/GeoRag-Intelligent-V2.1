import { test, expect } from '@playwright/test';

/**
 * Map layer toggle: visibility persists across reload (v1.5-11).
 *
 * jsdom can't compute MapLibre layer-visibility paint state, and the
 * localStorage round-trip in MapView.tsx is only meaningful with a
 * real browser session. Real-browser only.
 */

const BASE_URL = process.env.E2E_BASE_URL ?? 'http://localhost:8888';
const TEST_EMAIL = process.env.E2E_USER_EMAIL ?? 'demo@georag.dev';
const TEST_PASSWORD = process.env.E2E_USER_PASSWORD ?? 'password';
const STORAGE_KEY = 'georag:map_layer_visibility:v1';

test.describe('Map: layer visibility persists across reload', () => {
    test.beforeEach(async ({ page }) => {
        await page.goto(`${BASE_URL}/login`);
        await page.getByLabel(/email/i).fill(TEST_EMAIL);
        await page.getByLabel(/password/i).fill(TEST_PASSWORD);
        await page.getByRole('button', { name: /log in|sign in/i }).click();
        await page.waitForURL(/\/(dashboard|portfolio|map)/, { timeout: 10_000 });
    });

    test('toggling a layer off survives a hard reload', async ({ page, context }) => {
        await page.goto(`${BASE_URL}/map`);
        await page.waitForSelector('canvas.maplibregl-canvas', { timeout: 15_000 });
        await page.waitForTimeout(1500);

        // Open the layer panel and flip the collars layer off.
        await page.getByRole('button', { name: /layers/i }).click();
        const collarToggle = page.getByRole('switch', { name: /collars|drillhole.*collar/i });
        await expect(collarToggle).toBeVisible();
        const wasOn = (await collarToggle.getAttribute('aria-checked')) === 'true';
        if (wasOn) {
            await collarToggle.click();
        }
        await expect(collarToggle).toHaveAttribute('aria-checked', 'false');

        // Verify the value landed in localStorage with the v1-prefixed key.
        const stored = await page.evaluate(
            (key) => window.localStorage.getItem(key),
            STORAGE_KEY,
        );
        expect(stored).toBeTruthy();
        expect(JSON.parse(stored!).collars).toBe(false);

        // Hard reload — visibility must rehydrate from localStorage.
        await page.reload();
        await page.waitForSelector('canvas.maplibregl-canvas', { timeout: 15_000 });
        await page.waitForTimeout(1500);

        await page.getByRole('button', { name: /layers/i }).click();
        const collarToggleAfter = page.getByRole('switch', { name: /collars|drillhole.*collar/i });
        await expect(collarToggleAfter).toHaveAttribute('aria-checked', 'false');
    });

    test('tampered localStorage value falls back to default visibility', async ({ page }) => {
        await page.goto(`${BASE_URL}/map`);
        // Plant garbage at the storage key, then reload.
        await page.evaluate(
            (key) => window.localStorage.setItem(key, '{not json'),
            STORAGE_KEY,
        );
        await page.reload();
        await page.waitForSelector('canvas.maplibregl-canvas', { timeout: 15_000 });

        // Sanity: page didn't crash + the layer panel renders.
        await page.getByRole('button', { name: /layers/i }).click();
        await expect(page.getByRole('switch', { name: /collars/i })).toBeVisible();
    });
});
