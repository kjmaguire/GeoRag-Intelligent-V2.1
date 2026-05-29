import { test, expect } from '@playwright/test';

/**
 * Chat audit — Part B of the GeoRAG frontend audit.
 *
 * Companion to chat-citation.spec.ts, which already covers the
 * citation-click and refusal scenarios (B2 + B4 of the audit doc).
 * This file adds the remaining cross-cutting checks:
 *
 *   B1. Initial load — chat page mounts, composer focusable, no
 *       console errors before any interaction.
 *   B3. Streaming TTFT — measure time-to-first-visible-token from
 *       submit to the first non-empty assistant bubble.
 *   B5. Multi-turn — three sequential turns render distinct
 *       assistant bubbles without bleed.
 *   B6. Reverb WebSocket health — Echo connects, no socket-layer
 *       errors at page idle.
 *
 * Selectors match the production UI conventions established by
 * chat-citation.spec.ts: `data-role="assistant"`,
 * `data-streaming="false"`, `data-citation-marker`.
 */

const BASE_URL = process.env.E2E_BASE_URL ?? 'http://localhost:8888';
const TEST_EMAIL = process.env.E2E_USER_EMAIL ?? 'demo@georag.dev';
const TEST_PASSWORD = process.env.E2E_USER_PASSWORD ?? 'password';

// Console-noise we deliberately ignore. ResizeObserver loops fire from
// MapLibre and shadcn portals; DevTools messages aren't real errors;
// favicon 404s are cosmetic.
const NOISE_PATTERNS = [
    /ResizeObserver/i,
    /DevTools/i,
    /favicon/i,
];

const isReal = (msg: string) => !NOISE_PATTERNS.some((p) => p.test(msg));

async function login(page: import('@playwright/test').Page) {
    await page.goto(`${BASE_URL}/login`);
    await page.getByLabel(/email/i).fill(TEST_EMAIL);
    await page.getByLabel(/password/i).fill(TEST_PASSWORD);
    await page.getByRole('button', { name: /log in|sign in/i }).click();
    await page.waitForURL(/\/(dashboard|chat|portfolio)/, { timeout: 10_000 });
}

test.describe('Chat audit — Part B (B1, B3, B5, B6)', () => {
    test.beforeEach(async ({ page }) => {
        await login(page);
    });

    test('B1: chat page loads, composer focusable, no pre-interaction console errors', async ({ page }) => {
        const consoleErrors: string[] = [];
        const pageErrors: string[] = [];

        page.on('console', (msg) => {
            if (msg.type() === 'error') consoleErrors.push(msg.text());
        });
        page.on('pageerror', (err) => pageErrors.push(err.message));

        await page.goto(`${BASE_URL}/chat`, { waitUntil: 'networkidle' });

        const composer = page.getByRole('textbox', { name: /message|ask/i });
        await expect(composer).toBeVisible();
        await composer.click();
        await expect(composer).toBeFocused();

        await page.screenshot({
            path: 'tests/e2e/screenshots/chat-initial-load.png',
            fullPage: true,
        });

        const real = [...consoleErrors, ...pageErrors].filter(isReal);
        expect(real, `Console errors at idle:\n${real.join('\n')}`).toHaveLength(0);
    });

    test('B3: streaming TTFT — first assistant token visible within target', async ({ page }) => {
        await page.goto(`${BASE_URL}/chat`, { waitUntil: 'networkidle' });

        const composer = page.getByRole('textbox', { name: /message|ask/i });
        await composer.fill('Summarise the exploration history of this property.');

        const submittedAt = Date.now();
        await page.getByRole('button', { name: /send|submit/i }).click();

        // First-token signal: an assistant bubble with non-empty text.
        await page.waitForFunction(
            () => {
                const bubble = document.querySelector('[data-role="assistant"]');
                return bubble && (bubble.textContent ?? '').trim().length > 5;
            },
            { timeout: 10_000 },
        );

        const ttft = Date.now() - submittedAt;
        // eslint-disable-next-line no-console
        console.log(`TTFT (time to first token): ${ttft}ms`);

        // Warm-path target: < 3000ms. Cold path may exceed; record but
        // don't fail unless > 10s (which would suggest a pipeline stall).
        expect(ttft, `TTFT ${ttft}ms is excessive (> 10s suggests stall)`).toBeLessThan(10_000);

        // Soft target — log a warning at 3s without failing.
        if (ttft > 3000) {
            // eslint-disable-next-line no-console
            console.warn(`⚠️  TTFT ${ttft}ms exceeds 3000ms warm-path target`);
        }
    });

    test('B5: multi-turn conversation renders three distinct assistant bubbles', async ({ page }) => {
        const consoleErrors: string[] = [];
        page.on('console', (msg) => {
            if (msg.type() === 'error') consoleErrors.push(msg.text());
        });

        await page.goto(`${BASE_URL}/chat`, { waitUntil: 'networkidle' });

        const questions = [
            'What drillholes are in this project?',
            'Which one is the deepest?',
            'What was the best gold intersection in that hole?',
        ];

        for (const [i, q] of questions.entries()) {
            const composer = page.getByRole('textbox', { name: /message|ask/i });
            await composer.fill(q);
            await page.getByRole('button', { name: /send|submit/i }).click();

            // Wait for THIS turn's assistant bubble to finish streaming
            // before sending the next question. The (i+1)-th
            // data-streaming="false" bubble is the one we just produced.
            await page.waitForFunction(
                (expectedCount) => {
                    const done = document.querySelectorAll(
                        '[data-role="assistant"][data-streaming="false"]',
                    );
                    return done.length >= expectedCount;
                },
                i + 1,
                { timeout: 60_000 },
            );
        }

        const assistantBubbles = page.locator(
            '[data-role="assistant"][data-streaming="false"]',
        );
        expect(await assistantBubbles.count()).toBeGreaterThanOrEqual(3);

        const userBubbles = page.locator('[data-role="user"]');
        expect(await userBubbles.count()).toBeGreaterThanOrEqual(3);

        await page.screenshot({
            path: 'tests/e2e/screenshots/chat-multi-turn.png',
            fullPage: true,
        });

        const real = consoleErrors.filter(isReal);
        expect(real, `Console errors across turns:\n${real.join('\n')}`).toHaveLength(0);
    });

    test('B6: Reverb WebSocket connects without socket-layer errors', async ({ page }) => {
        const wsErrors: string[] = [];
        page.on('console', (msg) => {
            const text = msg.text();
            const isWs =
                /WebSocket|Pusher|Echo|socket|reverb/i.test(text) &&
                msg.type() === 'error';
            if (isWs) wsErrors.push(text);
        });

        await page.goto(`${BASE_URL}/chat`, { waitUntil: 'networkidle' });

        // Give Echo a moment to perform its subscribe handshake.
        await page.waitForTimeout(2_000);

        const wsState = await page.evaluate(() => {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const echo = (window as any).Echo;
            if (!echo) return { found: false };
            const state = echo.connector?.pusher?.connection?.state;
            return { found: true, state };
        });

        // eslint-disable-next-line no-console
        console.log('Echo connection state:', wsState);

        expect(wsState.found, 'window.Echo not exposed by app').toBe(true);
        expect(wsState.state, `Echo connection state is "${wsState.state}"`).toBe(
            'connected',
        );
        expect(wsErrors, `Socket-layer console errors:\n${wsErrors.join('\n')}`).toHaveLength(0);
    });
});
