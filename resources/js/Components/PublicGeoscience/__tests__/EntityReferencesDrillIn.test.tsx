import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import EntityReferencesDrillIn from '../EntityReferencesDrillIn';
import type { EntityReferencesResponse, SourceData } from '@/types';

const fakeResponse: EntityReferencesResponse = {
    canonical_type: 'mineral_occurrence',
    pg_id: 'abc-123',
    total: 2,
    min_confidence: 0.6,
    documents: [
        {
            document_id: 'doc-1',
            title: 'NI 43-101 Cigar Lake 2024',
            filename: 'cigar_lake_2024.pdf',
            filing_date: '2024-03-15',
            company: 'Cameco',
            commodity: 'Uranium',
            confidence: 0.95,
            signals: ['smdi_id_match'],
            extracted_context: '… SMDI 2661 is described in section 7 …',
            established_at: '2026-04-15T01:00:00Z',
            established_by: 'linker-v1',
        },
        {
            document_id: 'doc-2',
            title: 'Star Lake Assessment Report',
            filename: 'star_lake.pdf',
            filing_date: '2023-09-01',
            company: 'Junior Co',
            commodity: 'Gold',
            confidence: 0.72,
            signals: ['drillhole_id_match', 'nts_filename_match'],
            extracted_context: null,
            established_at: '2026-04-15T01:00:00Z',
            established_by: 'linker-v1',
        },
    ],
};

describe('<EntityReferencesDrillIn /> empty state (plan §07d clean-empty)', () => {
    it('renders the quiet empty hint when references_summary.count == 0', () => {
        render(
            <EntityReferencesDrillIn
                canonicalType="mineral_occurrence"
                pgId="abc-123"
                summary={{ count: 0, documents: [] }}
            />
        );
        expect(screen.getByText(/No assessment reports reference this record yet/)).toBeInTheDocument();
        expect(screen.queryByRole('button')).toBeNull();
    });

    it('renders the empty hint when summary is null', () => {
        render(
            <EntityReferencesDrillIn
                canonicalType="mine"
                pgId="abc-123"
                summary={null as unknown as SourceData['references_summary']}
            />
        );
        expect(screen.getByText(/No assessment reports reference this record yet/)).toBeInTheDocument();
    });
});

describe('<EntityReferencesDrillIn /> collapsed → expand', () => {
    let originalFetch: typeof globalThis.fetch;
    let fetchMock: ReturnType<typeof vi.fn>;

    beforeEach(() => {
        originalFetch = globalThis.fetch;
        fetchMock = vi.fn().mockResolvedValue({
            ok: true,
            status: 200,
            json: async () => fakeResponse,
        });
        globalThis.fetch = fetchMock as any;
    });

    afterEach(() => {
        globalThis.fetch = originalFetch;
        vi.clearAllMocks();
    });

    it('shows "Referenced in N reports" button collapsed by default', () => {
        render(
            <EntityReferencesDrillIn
                canonicalType="mineral_occurrence"
                pgId="abc-123"
                summary={{ count: 3, documents: [] }}
            />
        );
        expect(screen.getByText('Referenced in 3 assessment reports')).toBeInTheDocument();
        expect(globalThis.fetch).not.toHaveBeenCalled();
    });

    it('singularizes label when count == 1', () => {
        render(
            <EntityReferencesDrillIn
                canonicalType="mineral_occurrence"
                pgId="abc-123"
                summary={{ count: 1, documents: [] }}
            />
        );
        expect(screen.getByText('Referenced in 1 assessment report')).toBeInTheDocument();
    });

    it('lazy-fetches the references endpoint on first expand', async () => {
        render(
            <EntityReferencesDrillIn
                canonicalType="mineral_occurrence"
                pgId="abc-123"
                summary={{ count: 2, documents: [] }}
            />
        );
        fireEvent.click(screen.getByRole('button', { name: /Referenced in 2/ }));

        await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
        const url = fetchMock.mock.calls[0][0] as string;
        expect(url).toContain('/api/v1/public-geoscience/entities/mineral_occurrence/abc-123/references');
        expect(url).toContain('min_confidence=0.6');
    });

    it('renders fetched documents after expand', async () => {
        render(
            <EntityReferencesDrillIn
                canonicalType="mineral_occurrence"
                pgId="abc-123"
                summary={{ count: 2, documents: [] }}
            />
        );
        fireEvent.click(screen.getByRole('button', { name: /Referenced in 2/ }));

        await waitFor(() => {
            expect(screen.getByText('NI 43-101 Cigar Lake 2024')).toBeInTheDocument();
            expect(screen.getByText('Star Lake Assessment Report')).toBeInTheDocument();
        });
    });

    it('renders confidence chips per plan §07d gating', async () => {
        render(
            <EntityReferencesDrillIn
                canonicalType="mineral_occurrence"
                pgId="abc-123"
                summary={{ count: 2, documents: [] }}
            />
        );
        fireEvent.click(screen.getByRole('button', { name: /Referenced in 2/ }));

        // High confidence (≥ 0.9) → "match" label
        await waitFor(() => expect(screen.getByText(/match · 95%/)).toBeInTheDocument());
        // Likely (0.6 ≤ x < 0.9) → "likely match"
        expect(screen.getByText(/likely match · 72%/)).toBeInTheDocument();
    });

    it('renders signals as monospace chips', async () => {
        render(
            <EntityReferencesDrillIn
                canonicalType="mineral_occurrence"
                pgId="abc-123"
                summary={{ count: 2, documents: [] }}
            />
        );
        fireEvent.click(screen.getByRole('button', { name: /Referenced in 2/ }));

        await waitFor(() => expect(screen.getByText('smdi_id_match')).toBeInTheDocument());
        expect(screen.getByText('drillhole_id_match')).toBeInTheDocument();
        expect(screen.getByText('nts_filename_match')).toBeInTheDocument();
    });

    it('renders extracted_context when present', async () => {
        render(
            <EntityReferencesDrillIn
                canonicalType="mineral_occurrence"
                pgId="abc-123"
                summary={{ count: 2, documents: [] }}
            />
        );
        fireEvent.click(screen.getByRole('button', { name: /Referenced in 2/ }));

        await waitFor(() => {
            expect(screen.getByText(/SMDI 2661 is described/)).toBeInTheDocument();
        });
    });

    it('toggles back to collapsed on second click', async () => {
        render(
            <EntityReferencesDrillIn
                canonicalType="mineral_occurrence"
                pgId="abc-123"
                summary={{ count: 2, documents: [] }}
            />
        );
        fireEvent.click(screen.getByRole('button', { name: /Referenced in 2/ }));
        await waitFor(() => expect(screen.getByText(/Hide references/)).toBeInTheDocument());
        fireEvent.click(screen.getByText(/Hide references/));
        expect(screen.getByText(/Referenced in 2/)).toBeInTheDocument();
    });
});

describe('<EntityReferencesDrillIn /> include-possible toggle', () => {
    let originalFetch: typeof globalThis.fetch;
    let fetchMock: ReturnType<typeof vi.fn>;

    beforeEach(() => {
        originalFetch = globalThis.fetch;
        fetchMock = vi.fn().mockResolvedValue({
            ok: true,
            status: 200,
            json: async () => fakeResponse,
        });
        globalThis.fetch = fetchMock as any;
    });

    afterEach(() => {
        globalThis.fetch = originalFetch;
        vi.clearAllMocks();
    });

    it('refetches with min_confidence=0.4 when "Include lower-confidence" is checked', async () => {
        render(
            <EntityReferencesDrillIn
                canonicalType="mineral_occurrence"
                pgId="abc-123"
                summary={{ count: 2, documents: [] }}
            />
        );
        fireEvent.click(screen.getByRole('button', { name: /Referenced in 2/ }));
        await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

        const checkbox = screen.getByRole('checkbox', { name: /lower-confidence/i });
        fireEvent.click(checkbox);

        await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
        const lastUrl = fetchMock.mock.calls[1][0] as string;
        expect(lastUrl).toContain('min_confidence=0.4');
    });
});

describe('<EntityReferencesDrillIn /> error path', () => {
    let originalFetch: typeof globalThis.fetch;

    afterEach(() => {
        globalThis.fetch = originalFetch;
        vi.clearAllMocks();
    });

    it('shows a "Failed to load" message on HTTP error', async () => {
        originalFetch = globalThis.fetch;
        globalThis.fetch = vi.fn().mockResolvedValue({ ok: false, status: 500 }) as any;
        render(
            <EntityReferencesDrillIn
                canonicalType="mineral_occurrence"
                pgId="abc-123"
                summary={{ count: 2, documents: [] }}
            />
        );
        fireEvent.click(screen.getByRole('button', { name: /Referenced in 2/ }));
        await waitFor(() => {
            expect(screen.getByText(/Failed to load/)).toBeInTheDocument();
        });
    });
});

// ── Auth surface (security regression) ────────────────────────────────────────

describe('<EntityReferencesDrillIn /> auth surface', () => {
    let originalFetch: typeof globalThis.fetch;
    let fetchMock: ReturnType<typeof vi.fn>;

    beforeEach(() => {
        originalFetch = globalThis.fetch;
        fetchMock = vi.fn().mockResolvedValue({
            ok: true,
            status: 200,
            json: async () => fakeResponse,
        });
        globalThis.fetch = fetchMock as any;
    });

    afterEach(() => {
        globalThis.fetch = originalFetch;
        vi.clearAllMocks();
    });

    it('does not read auth tokens from localStorage when fetching references', async () => {
        const getItemSpy = vi.spyOn(Storage.prototype, 'getItem');
        render(
            <EntityReferencesDrillIn
                canonicalType="mineral_occurrence"
                pgId="abc-123"
                summary={{ count: 2, documents: [] }}
            />
        );
        fireEvent.click(screen.getByRole('button', { name: /Referenced in 2/ }));
        await waitFor(() => expect(fetchMock).toHaveBeenCalled());

        const tokenLike = /token|jwt|secret/i;
        const offendingKeys = getItemSpy.mock.calls
            .map((call) => String(call[0]))
            .filter((key) => tokenLike.test(key));
        expect(offendingKeys).toEqual([]);
        getItemSpy.mockRestore();
    });

    it('sends the references fetch with same-origin credentials', async () => {
        render(
            <EntityReferencesDrillIn
                canonicalType="mineral_occurrence"
                pgId="abc-123"
                summary={{ count: 2, documents: [] }}
            />
        );
        fireEvent.click(screen.getByRole('button', { name: /Referenced in 2/ }));
        await waitFor(() => expect(fetchMock).toHaveBeenCalled());

        const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
        expect(init?.credentials).toBe('same-origin');
        const headers = (init?.headers ?? {}) as Record<string, string>;
        expect(headers.Authorization).toBeUndefined();
    });
});
