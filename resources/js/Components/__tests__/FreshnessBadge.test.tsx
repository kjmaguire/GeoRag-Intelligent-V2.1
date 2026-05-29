// @ts-nocheck
/**
 * FreshnessBadge.test.tsx
 *
 * Tests for the B8 FreshnessBadge component and its computeStaleness helper.
 *
 * Coverage:
 *   - Fresh: answered_at < 24h ago → "Fresh" badge, green
 *   - Recent: 24h–7d → "Recent" badge, amber
 *   - Stale: >7d → "Stale" badge, red
 *   - Missing freshness renders nothing
 *   - Missing answered_at renders nothing
 *   - aria-label contains staleness class + date
 *   - data-staleness attribute for test queries
 */

import { describe, it, expect, beforeAll, afterAll, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

// V1.5-20 — FreshnessBadge now reads `workspace.data_version` from
// Inertia's usePage(). Mock the module so render tests don't need a
// real <InertiaApp> provider. Each test that wants a specific version
// overrides via vi.mocked(usePage).mockReturnValue(...).
vi.mock('@inertiajs/react', () => ({
    usePage: vi.fn(() => ({ props: { workspace: undefined } })),
}));

import { usePage } from '@inertiajs/react';
import { FreshnessBadge, computeStaleness } from '../chat/FreshnessBadge';
import type { FreshnessData } from '../chat/FreshnessBadge';

// ── Time helpers ──────────────────────────────────────────────────────────

const NOW = new Date('2026-04-22T12:00:00Z').getTime();

// Pin the system clock so render-based tests (which call Date.now() inside
// the component) see the same NOW as the fixture-based isoAt() helper.
// Without this, date-driven assertions flake once real time advances past
// the hard-coded NOW (originally hit during Module 8 Chunk 8.5 verification).
beforeAll(() => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    vi.setSystemTime(new Date(NOW));
});
afterAll(() => {
    vi.useRealTimers();
});
const ONE_HOUR_MS   = 60 * 60 * 1000;
const ONE_DAY_MS    = 24 * ONE_HOUR_MS;
const EIGHT_DAYS_MS = 8  * ONE_DAY_MS;

function isoAt(offsetMs: number): string {
    return new Date(NOW - offsetMs).toISOString();
}

function makeFreshness(
    answeredAt: string,
    workspaceDataVersionAtQuery: number = 42,
): FreshnessData {
    return {
        workspace_data_version_at_query: workspaceDataVersionAtQuery,
        project_data_version_at_query: null,
        answered_at: answeredAt,
    };
}

// ── computeStaleness unit tests ────────────────────────────────────────────

describe('computeStaleness — unit', () => {
    it('returns "fresh" when age is < 24h', () => {
        const result = computeStaleness(isoAt(ONE_HOUR_MS), NOW);
        expect(result.cls).toBe('fresh');
        expect(result.label).toBe('Fresh');
    });

    it('returns "recent" when age is exactly 24h', () => {
        const result = computeStaleness(isoAt(ONE_DAY_MS), NOW);
        expect(result.cls).toBe('recent');
    });

    it('returns "recent" when age is 3 days', () => {
        const result = computeStaleness(isoAt(3 * ONE_DAY_MS), NOW);
        expect(result.cls).toBe('recent');
        expect(result.label).toBe('Recent');
    });

    it('returns "stale" when age is > 7d', () => {
        const result = computeStaleness(isoAt(EIGHT_DAYS_MS), NOW);
        expect(result.cls).toBe('stale');
        expect(result.label).toBe('Stale');
    });

    it('ariaLabel contains the answered_at date', () => {
        const answeredAt = isoAt(ONE_HOUR_MS);
        const result = computeStaleness(answeredAt, NOW);
        expect(result.ariaLabel).toContain(answeredAt);
    });
});

// ── Component render tests ────────────────────────────────────────────────

describe('FreshnessBadge — fresh', () => {
    it('renders "Fresh" text for a recent answer', () => {
        const { getByTestId } = render(
            <FreshnessBadge freshness={makeFreshness(isoAt(ONE_HOUR_MS))} />
        );
        const badge = getByTestId('freshness-badge');
        expect(badge.textContent).toContain('Fresh');
        expect(badge.getAttribute('data-staleness')).toBe('fresh');
    });

    it('has an aria-label containing "fresh" for recent answers', () => {
        render(<FreshnessBadge freshness={makeFreshness(isoAt(ONE_HOUR_MS))} />);
        const badge = screen.getByTestId('freshness-badge');
        expect(badge.getAttribute('aria-label')?.toLowerCase()).toContain('fresh');
    });
});

describe('FreshnessBadge — recent', () => {
    it('renders "Recent" text for 3-day-old answers', () => {
        render(<FreshnessBadge freshness={makeFreshness(isoAt(3 * ONE_DAY_MS))} />);
        const badge = screen.getByTestId('freshness-badge');
        expect(badge.textContent).toContain('Recent');
        expect(badge.getAttribute('data-staleness')).toBe('recent');
    });
});

describe('FreshnessBadge — stale', () => {
    it('renders "Stale" text for 8-day-old answers', () => {
        render(<FreshnessBadge freshness={makeFreshness(isoAt(EIGHT_DAYS_MS))} />);
        const badge = screen.getByTestId('freshness-badge');
        expect(badge.textContent).toContain('Stale');
        expect(badge.getAttribute('data-staleness')).toBe('stale');
    });

    it('aria-label mentions stale for old answers', () => {
        render(<FreshnessBadge freshness={makeFreshness(isoAt(EIGHT_DAYS_MS))} />);
        const badge = screen.getByTestId('freshness-badge');
        expect(badge.getAttribute('aria-label')?.toLowerCase()).toContain('stale');
    });
});

// ── Missing freshness data ─────────────────────────────────────────────────

describe('FreshnessBadge — missing data', () => {
    it('renders nothing when freshness is null', () => {
        const { container } = render(<FreshnessBadge freshness={null} />);
        expect(container.firstChild).toBeNull();
    });

    it('renders nothing when freshness is undefined', () => {
        const { container } = render(<FreshnessBadge freshness={undefined} />);
        expect(container.firstChild).toBeNull();
    });

    it('renders nothing when answered_at is missing', () => {
        const { container } = render(
            <FreshnessBadge freshness={{ workspace_data_version_at_query: 1 } as any} />
        );
        expect(container.firstChild).toBeNull();
    });
});

// ── V1.5-20: data_version diff signal ──────────────────────────────────────

describe('computeStaleness — data_version diff (V1.5-20)', () => {
    it('returns "stale" when current > query data_version (even within 24h)', () => {
        const result = computeStaleness(
            isoAt(ONE_HOUR_MS), // 1h ago — would be "fresh" by clock alone
            NOW,
            10,                  // query_data_version
            11,                  // current_data_version (corpus advanced)
        );
        expect(result.cls).toBe('stale');
        expect(result.ariaLabel).toContain('data_version advanced');
        expect(result.ariaLabel).toContain('1');
    });

    it('falls back to clock age when current == query data_version', () => {
        const result = computeStaleness(
            isoAt(ONE_HOUR_MS),
            NOW,
            42,
            42,
        );
        expect(result.cls).toBe('fresh');
    });

    it('falls back to clock age when query_data_version is null', () => {
        const result = computeStaleness(
            isoAt(EIGHT_DAYS_MS),
            NOW,
            null,
            42,
        );
        expect(result.cls).toBe('stale'); // > 7d clock age
    });

    it('falls back to clock age when current_data_version is undefined', () => {
        const result = computeStaleness(
            isoAt(ONE_HOUR_MS),
            NOW,
            10,
            undefined,
        );
        expect(result.cls).toBe('fresh');
    });

    it('reports the diff size in the aria-label', () => {
        const result = computeStaleness(
            isoAt(ONE_HOUR_MS),
            NOW,
            5,
            8,
        );
        expect(result.cls).toBe('stale');
        expect(result.ariaLabel).toContain('advanced by 3');
    });
});

// ── V1.5-20: FreshnessBadge consumes Inertia workspace.data_version ────────

describe('FreshnessBadge — workspace data_version diff (V1.5-20)', () => {
    it('renders "Stale" when current workspace.data_version > query', () => {
        vi.mocked(usePage).mockReturnValue({
            props: { workspace: { id: 'w-1', name: 'W', data_version: 11 } },
        } as any);

        render(<FreshnessBadge freshness={makeFreshness(isoAt(ONE_HOUR_MS), 10)} />);

        const badge = screen.getByTestId('freshness-badge');
        expect(badge.getAttribute('data-staleness')).toBe('stale');
        expect(badge.textContent).toContain('Stale');
        expect(badge.getAttribute('aria-label')).toContain('data_version advanced');
    });

    it('renders "Fresh" when current workspace.data_version == query', () => {
        vi.mocked(usePage).mockReturnValue({
            props: { workspace: { id: 'w-1', name: 'W', data_version: 42 } },
        } as any);

        render(<FreshnessBadge freshness={makeFreshness(isoAt(ONE_HOUR_MS), 42)} />);

        const badge = screen.getByTestId('freshness-badge');
        expect(badge.getAttribute('data-staleness')).toBe('fresh');
    });

    it('falls back to clock age when workspace prop is absent', () => {
        vi.mocked(usePage).mockReturnValue({ props: {} } as any);

        render(<FreshnessBadge freshness={makeFreshness(isoAt(ONE_HOUR_MS), 10)} />);

        const badge = screen.getByTestId('freshness-badge');
        // Without current data_version, falls back to clock — 1h ago is fresh.
        expect(badge.getAttribute('data-staleness')).toBe('fresh');
    });
});
