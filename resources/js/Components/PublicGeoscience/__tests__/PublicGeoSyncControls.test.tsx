import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import PublicGeoSyncControls, { formatFreshness } from '../PublicGeoSyncControls';
import PolygonLayerToggles from '../PolygonLayerToggles';

const pushToast = vi.fn();
vi.mock('@/Components/Foundry/ToastHost', () => ({
    useToast: () => ({ pushToast }),
}));

const STATUS = {
    layers: [
        { layer: 'mine', jurisdiction_code: 'CA-SK', rows: 140, last_seen_at: '2026-09-27T18:45:00+00:00' },
        { layer: 'mineral_disposition', jurisdiction_code: 'CA-SK', rows: 30906, last_seen_at: '2026-09-27T19:10:00+00:00' },
        { layer: 'mineral_occurrence', jurisdiction_code: 'CA-BC', rows: 15000, last_seen_at: '2026-09-27T20:00:00+00:00' },
    ],
    last_seen_at: '2026-09-27T20:00:00+00:00',
};

function jsonResponse(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

describe('<PublicGeoSyncControls />', () => {
    let fetchMock: ReturnType<typeof vi.fn>;

    beforeEach(() => {
        pushToast.mockReset();
        fetchMock = vi.fn((url: string) =>
            Promise.resolve(url.endsWith('/sync-status') ? jsonResponse(STATUS) : jsonResponse({}, 500)),
        );
        vi.stubGlobal('fetch', fetchMock);
    });

    afterEach(() => {
        vi.unstubAllGlobals();
    });

    it('hides "Sync now" from non-admins but still shows freshness', async () => {
        render(<PublicGeoSyncControls isAdmin={false} />);
        expect(await screen.findByTestId('pg-freshness')).toHaveTextContent('CA-BC 15,000 · CA-SK 31,046');
        expect(screen.queryByRole('button', { name: /sync now/i })).toBeNull();
    });

    it('queues a sync for an admin and toasts the run id', async () => {
        fetchMock.mockImplementation((url: string, init?: RequestInit) => {
            if (url.endsWith('/sync') && init?.method === 'POST') {
                return Promise.resolve(jsonResponse({ workflow_run_id: 'run-42', feeds: 36 }, 202));
            }
            return Promise.resolve(jsonResponse(STATUS));
        });

        render(<PublicGeoSyncControls isAdmin />);
        fireEvent.click(screen.getByRole('button', { name: /sync now/i }));

        await waitFor(() => expect(pushToast).toHaveBeenCalled());
        const toast = pushToast.mock.calls[0][0];
        expect(toast.title).toBe('Public geo sync queued');
        expect(toast.detail).toContain('Sync started · 36 feeds');
        expect(toast.detail).not.toMatch(/run-42|Hatchet/);
        const post = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === 'POST');
        expect(post?.[0]).toBe('/api/v1/public-geoscience/sync');
    });

    it('explains a cooldown 429 with the run already queued', async () => {
        fetchMock.mockImplementation((url: string, init?: RequestInit) =>
            Promise.resolve(
                init?.method === 'POST'
                    ? jsonResponse({ error: 'sync_recently_triggered', workflow_run_id: 'run-first' }, 429)
                    : jsonResponse(STATUS),
            ),
        );

        render(<PublicGeoSyncControls isAdmin />);
        fireEvent.click(screen.getByRole('button', { name: /sync now/i }));

        await waitFor(() => expect(pushToast).toHaveBeenCalled());
        expect(pushToast.mock.calls[0][0]).toMatchObject({ tone: 'warn', detail: 'A sync is already queued — results will update when it finishes.' });
    });

    it('surfaces a server refusal', async () => {
        fetchMock.mockImplementation((url: string, init?: RequestInit) =>
            Promise.resolve(
                init?.method === 'POST' ? jsonResponse({ message: 'FastAPI unreachable' }, 502) : jsonResponse(STATUS),
            ),
        );

        render(<PublicGeoSyncControls isAdmin />);
        fireEvent.click(screen.getByRole('button', { name: /sync now/i }));

        await waitFor(() => expect(pushToast).toHaveBeenCalled());
        expect(pushToast.mock.calls[0][0]).toMatchObject({ title: 'Sync could not be started', detail: 'FastAPI unreachable' });
    });
});

describe('formatFreshness', () => {
    it('reports an empty mirror plainly', () => {
        expect(formatFreshness({ layers: [], last_seen_at: null })).toBe('Not synced yet');
        expect(formatFreshness(null)).toBeNull();
    });
});

describe('<PolygonLayerToggles />', () => {
    it('toggles layers in canonical order and shows cap / zoom status', () => {
        const onChange = vi.fn();
        render(
            <PolygonLayerToggles
                enabled={['mineral_disposition']}
                onChange={onChange}
                meta={{
                    mineral_disposition: {
                        mode: 'polygons', min_zoom: 6, total_in_view: 30906, returned: 1500, truncated: true,
                    },
                }}
            />,
        );

        expect(screen.getByLabelText('Mineral tenure')).toBeChecked();
        expect(screen.getByLabelText('Bedrock geology')).not.toBeChecked();
        expect(screen.getByText(/zoom in for all/)).toBeInTheDocument();

        fireEvent.click(screen.getByLabelText('Bedrock geology'));
        expect(onChange).toHaveBeenCalledWith(['mineral_disposition', 'bedrock_geology']);

        fireEvent.click(screen.getByLabelText('Mineral tenure'));
        expect(onChange).toHaveBeenLastCalledWith([]);
    });
});
