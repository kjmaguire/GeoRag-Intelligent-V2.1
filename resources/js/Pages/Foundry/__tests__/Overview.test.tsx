/**
 * Foundry/Overview - the ingestion tile (FE-8).
 *
 * The tile was seeded from the `ingest_summary` prop once, so the fresh value an
 * ingest-complete partial reload delivers was dropped; and its poll chain stopped
 * for good when a tick ended with nothing in flight and the tab hidden - nothing
 * started it again, so a tab left in the background came back showing a stale tile.
 */
import { act, cleanup, render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@inertiajs/react', () => ({
    Head: () => null,
    Link: ({ href, children, ...rest }: { href: string; children: ReactNode }) => (
        <a href={href} {...rest}>
            {children}
        </a>
    ),
    router: { reload: vi.fn(), visit: vi.fn() },
}));
vi.mock('@/Hooks/useWorkspaceDataUpdated', () => ({ useWorkspaceDataUpdated: () => {} }));
vi.mock('@/Components/EditProjectSheet', () => ({ default: () => null }));

import FoundryOverview from '../Overview';

type Summary = { in_flight: number; completed: number; latest_in_flight: string | null };

function props(ingest_summary: Summary) {
    return {
        project: {
            project_id: 'p-1',
            project_name: 'Red Star',
            slug: 'red-star',
            region: null,
            commodity: null,
            company: null,
            status: 'active',
            crs_epsg: null,
            data_version: 1,
        },
        kpis: [],
        next_action: {
            title: 'Ask a question',
            detail: 'Try the chat.',
            cta: 'Open chat',
            href: '/projects/red-star/chat',
        },
        recent_activity: [],
        ingest_summary,
        ocr_coverage: {
            total: 0,
            ocr_total: 0,
            native_total: 0,
            unknown_total: 0,
            flagged_total: 0,
            by_method: [],
            measured_accuracy: null,
        },
        empty: false,
    };
}

const IDLE: Summary = { in_flight: 0, completed: 2, latest_in_flight: null };

/** What /ingestion-runs.json answers. */
let polled: { in_flight: number; completed: number; latest?: string | null };
let fetchSpy: ReturnType<typeof vi.spyOn>;
let visibility: DocumentVisibilityState;

function setVisibility(next: DocumentVisibilityState) {
    visibility = next;
    document.dispatchEvent(new Event('visibilitychange'));
}

/** Let `ms` of fake time pass, with the promises each timer starts running to completion. */
async function advance(ms: number) {
    await act(async () => {
        await vi.advanceTimersByTimeAsync(ms);
    });
}

const polls = () => fetchSpy.mock.calls.filter(([url]) => String(url).endsWith('/ingestion-runs.json')).length;

beforeEach(() => {
    vi.useFakeTimers();
    polled = { in_flight: 0, completed: 5 };
    visibility = 'visible';
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility });
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(
        async () =>
            new Response(
                JSON.stringify({
                    runs: {
                        totals: { in_flight: polled.in_flight, completed: polled.completed },
                        in_flight: [],
                        latest_in_flight: polled.latest ?? null,
                    },
                }),
            ),
    );
});

afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
    Reflect.deleteProperty(document, 'visibilityState');
});

describe('Overview ingestion tile', () => {
    it('shows the summary a reload delivers, instead of keeping the one it was first given', () => {
        const { rerender } = render(<FoundryOverview {...props(IDLE)} />);
        expect(screen.getByText(/2 documents ingested/)).toBeInTheDocument();
        expect(screen.queryByText(/ingesting/)).toBeNull();

        // An ingest started: the partial reload hands over a new ingest_summary.
        rerender(<FoundryOverview {...props({ in_flight: 2, completed: 2, latest_in_flight: 'collars.csv' })} />);

        expect(screen.getByText(/2 files ingesting/)).toBeInTheDocument();
        expect(screen.getByText(/latest: collars\.csv/)).toBeInTheDocument();
        expect(screen.queryByText(/documents ingested/)).toBeNull();
    });

    it('keeps what the poll found when an unrelated re-render brings back the same summary', async () => {
        const summary = IDLE;
        const { rerender } = render(<FoundryOverview {...props(summary)} />);
        await advance(30_000);
        expect(screen.getByText(/5 documents ingested/)).toBeInTheDocument();

        // Same prop object: not a new delivery, so the poll's newer figure stays.
        rerender(<FoundryOverview {...{ ...props(summary), ingest_summary: summary }} />);
        expect(screen.getByText(/5 documents ingested/)).toBeInTheDocument();
    });

    it('polls every 30 s while the tab is visible and nothing is in flight', async () => {
        render(<FoundryOverview {...props(IDLE)} />);
        expect(polls()).toBe(0);

        await advance(30_000);
        expect(polls()).toBe(1);
        await advance(30_000);
        expect(polls()).toBe(2);
    });

    it('polls every 5 s while something is in flight, even with the tab hidden', async () => {
        visibility = 'hidden';
        polled = { in_flight: 1, completed: 2, latest: 'a.pdf' };
        render(<FoundryOverview {...props({ in_flight: 1, completed: 2, latest_in_flight: 'a.pdf' })} />);

        await advance(5_000);
        expect(polls()).toBe(1);
        await advance(5_000);
        expect(polls()).toBe(2);
    });

    it('does not poll a hidden tab with nothing in flight, and refreshes the moment it is visible again', async () => {
        visibility = 'hidden';
        render(<FoundryOverview {...props(IDLE)} />);

        await advance(120_000);
        expect(polls()).toBe(0);
        expect(screen.getByText(/2 documents ingested/)).toBeInTheDocument();

        await act(async () => {
            setVisibility('visible');
            await vi.advanceTimersByTimeAsync(0);
        });
        expect(polls()).toBe(1);
        expect(screen.getByText(/5 documents ingested/)).toBeInTheDocument();

        // ...and the chain is back: the next poll follows 30 s later.
        await advance(30_000);
        expect(polls()).toBe(2);
    });

    it('resumes after a tick that finished while the tab was hidden', async () => {
        render(<FoundryOverview {...props(IDLE)} />);
        // Hidden before the first tick fires, so that tick ends hidden-and-idle.
        visibility = 'hidden';
        await advance(30_000);
        const before = polls();

        await advance(120_000);
        expect(polls()).toBe(before);

        await act(async () => {
            setVisibility('visible');
            await vi.advanceTimersByTimeAsync(0);
        });
        expect(polls()).toBe(before + 1);
    });

    it('never runs two polls at once when the tab flips visible while one is pending', async () => {
        let release: (r: Response) => void = () => {};
        fetchSpy.mockImplementation(
            () =>
                new Promise<Response>((resolve) => {
                    release = resolve;
                }),
        );
        render(<FoundryOverview {...props(IDLE)} />);
        await advance(30_000);
        expect(polls()).toBe(1);

        await act(async () => {
            setVisibility('hidden');
            setVisibility('visible');
            setVisibility('hidden');
            setVisibility('visible');
            await vi.advanceTimersByTimeAsync(0);
        });
        expect(polls()).toBe(1);

        await act(async () => {
            release(new Response(JSON.stringify({ runs: { totals: { in_flight: 0, completed: 5 } } })));
            await vi.advanceTimersByTimeAsync(0);
        });
        // One chain: exactly one more poll 30 s on, not one per flip.
        fetchSpy.mockClear();
        await advance(30_000);
        expect(polls()).toBe(1);
    });

    it('stops polling and listening when the page is left', async () => {
        const { unmount } = render(<FoundryOverview {...props(IDLE)} />);
        unmount();

        await advance(120_000);
        await act(async () => {
            setVisibility('visible');
            await vi.advanceTimersByTimeAsync(0);
        });
        expect(polls()).toBe(0);
    });
});
