import { test, expect } from '@playwright/test';

/**
 * Chat citation flow: ask a question → get an answer → click a citation
 * marker → evidence bubble renders with the chunk text.
 *
 * Why this is a real-browser test: the citation popover uses the
 * radix-ui Popover component, which renders into a portal. jsdom
 * approximates portals but doesn't compute pointer-events on stacked
 * portal layers. Real-browser only.
 */

const BASE_URL = process.env.E2E_BASE_URL ?? 'http://localhost:8888';
const TEST_EMAIL = process.env.E2E_USER_EMAIL ?? 'demo@georag.dev';
const TEST_PASSWORD = process.env.E2E_USER_PASSWORD ?? 'password';

test.describe('Chat: citation click → evidence bubble', () => {
    test.beforeEach(async ({ page }) => {
        await page.goto(`${BASE_URL}/login`);
        await page.getByLabel(/email/i).fill(TEST_EMAIL);
        await page.getByLabel(/password/i).fill(TEST_PASSWORD);
        await page.getByRole('button', { name: /log in|sign in/i }).click();
        await page.waitForURL(/\/(dashboard|chat|portfolio)/, { timeout: 10_000 });
    });

    test('answer renders citations and clicking one opens the evidence bubble', async ({ page }) => {
        await page.goto(`${BASE_URL}/chat`);

        const composer = page.getByRole('textbox', { name: /message|ask/i });
        await expect(composer).toBeVisible();
        await composer.fill('How many drill holes are in this project?');
        await page.getByRole('button', { name: /send|submit/i }).click();

        // SSE stream — wait for the assistant message bubble to materialise
        // and stop streaming. The `data-streaming="false"` flag is set
        // on the assistant bubble when the `completed` event lands.
        await page.waitForSelector(
            '[data-role="assistant"][data-streaming="false"]',
            { timeout: 60_000 },
        );

        // At least one citation marker should be present in the bubble.
        const markers = page.locator('[data-role="assistant"] [data-citation-marker]');
        const count = await markers.count();
        expect(count).toBeGreaterThan(0);

        // Clicking the first marker opens the evidence Popover.
        await markers.first().click();
        const popover = page.getByRole('dialog', { name: /evidence|citation/i });
        await expect(popover).toBeVisible({ timeout: 5_000 });

        // The bubble must contain the source chunk text and a stable
        // chunk-id (per Section 04i — citations are mandatory).
        await expect(popover.locator('[data-source-chunk-id]')).toHaveCount(1);
        const text = await popover.locator('[data-source-chunk-id]').textContent();
        expect(text?.trim().length ?? 0).toBeGreaterThan(20);
    });

    test('refusal answers do not crash the citation rendering', async ({ page }) => {
        // A query that should refuse — fabricated hole id (per Section 04i
        // hallucination prevention layer 4: entity resolution).
        await page.goto(`${BASE_URL}/chat`);
        await page.getByRole('textbox', { name: /message|ask/i }).fill(
            'What is the gold grade at hole XYZ-999-999?',
        );
        await page.getByRole('button', { name: /send|submit/i }).click();

        await page.waitForSelector(
            '[data-role="assistant"][data-streaming="false"]',
            { timeout: 60_000 },
        );

        // Refusal text shouldn't claim a number. Loose check: no
        // sentence containing "g/t" or "% Au" — those would imply a
        // grade was returned.
        const text = await page
            .locator('[data-role="assistant"]')
            .last()
            .textContent();
        expect(text).not.toMatch(/g\/t|% Au/i);
    });
});
