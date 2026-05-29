import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import CitationPGEODetail, { parseSourceChunkId } from '../CitationPGEODetail';
import type { Citation, SourceData } from '@/types';

// EntityReferencesDrillIn fetches a different endpoint when expanded; we mock
// it here to keep these tests focused on the parent card.
vi.mock('../EntityReferencesDrillIn', () => ({
    default: ({ canonicalType, pgId, summary }: any) => (
        <div data-testid="references-drill-in"
             data-canonical={canonicalType}
             data-pg-id={pgId ?? ''}
             data-count={summary?.count ?? 0}
        >
            references-drill-in
        </div>
    ),
}));

const baseCitation: Citation = {
    citation_id: '[PGEO-1]',
    citation_type: 'PGEO',
    source_chunk_id: 'pg_mineral_occurrence:CA-SK-SMDI:feature=2821:pg_id=4e8cdee5-e6d4-40b4-85c0-8a25e64659e3',
    document_title: "Saskatchewan -- Occurrence mineral occurrence 'GB Gold Showing' (SMDI 2661)",
    relevance_score: 0.84499145,
    corpus: 'public_geo',
    jurisdiction_code: 'CA-SK',
    jurisdiction_name: 'Saskatchewan',
    license_summary: 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0',
    license_url: 'https://example.gov.sk.ca/license',
    source_url: 'https://mineraldeposits.saskatchewan.ca/Home/Viewdetails/2661',
    staleness_seconds: 1985,
};

describe('parseSourceChunkId', () => {
    it('parses a full PGEO chunk id', () => {
        const r = parseSourceChunkId(baseCitation.source_chunk_id);
        expect(r).toEqual({
            canonical_type: 'mineral_occurrence',
            source_id: 'CA-SK-SMDI',
            feature_id: '2821',
            pg_id: '4e8cdee5-e6d4-40b4-85c0-8a25e64659e3',
        });
    });

    it('rejects unknown canonical_types (TS union guard)', () => {
        // The regex itself accepts [a-z0-9_]+ (matches a future
        // pg_resource_potential_v2 chunk id), but the parser's post-match
        // guard narrows to the four canonical_types the frontend knows
        // how to render. Anything else returns null so the chat panel
        // falls back to the legacy SourceViewer instead of crashing.
        // The Laravel resolver-side widening (Concern #10) is the
        // server-side analog and is tested in CitationControllerPgeoTest.
        expect(parseSourceChunkId('pg_resource_potential_v2:CA-SK-RP:feature=1:pg_id=abcd')).toBeNull();
        expect(parseSourceChunkId('pg_unknown_type:foo:feature=1:pg_id=x')).toBeNull();
    });

    it('returns null for non-PGEO chunk ids', () => {
        expect(parseSourceChunkId('silver.collars:count=20:first=abc')).toBeNull();
        expect(parseSourceChunkId('georag_reports:abc:section=1')).toBeNull();
    });

    it('handles missing optional segments', () => {
        const r = parseSourceChunkId('pg_mine:CA-SK-MINE-LOC');
        expect(r?.canonical_type).toBe('mine');
        expect(r?.source_id).toBe('CA-SK-MINE-LOC');
        expect(r?.feature_id).toBeNull();
        expect(r?.pg_id).toBeNull();
    });
});

describe('<CitationPGEODetail /> header', () => {
    beforeEach(() => {
        // Avoid noisy console.error from any wrapper that complains about act
        vi.spyOn(console, 'error').mockImplementation(() => {});
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('renders jurisdiction name in eyebrow', () => {
        render(<CitationPGEODetail citation={baseCitation} />);
        expect(screen.getByText('Saskatchewan')).toBeInTheDocument();
    });

    it('falls back to jurisdiction_code when name is missing', () => {
        render(<CitationPGEODetail citation={{ ...baseCitation, jurisdiction_name: null }} />);
        expect(screen.getByText('CA-SK')).toBeInTheDocument();
    });

    it('falls back to "Public Geoscience" when both are missing', () => {
        render(<CitationPGEODetail citation={{ ...baseCitation, jurisdiction_name: null, jurisdiction_code: null }} />);
        expect(screen.getByText('Public Geoscience')).toBeInTheDocument();
    });

    it('renders document_title as authority line before resolver loads', () => {
        render(<CitationPGEODetail citation={baseCitation} />);
        expect(screen.getByText(baseCitation.document_title)).toBeInTheDocument();
    });

    it('renders source host parsed from source_url', () => {
        render(<CitationPGEODetail citation={baseCitation} />);
        expect(screen.getByText(/mineraldeposits\.saskatchewan\.ca/)).toBeInTheDocument();
    });

    it('renders staleness label (e.g. "33 min ago")', () => {
        render(<CitationPGEODetail citation={baseCitation} />);
        expect(screen.getByText(/33 min ago/)).toBeInTheDocument();
    });
});

describe('<CitationPGEODetail /> license', () => {
    it('renders the license badge with copyright marker', () => {
        render(<CitationPGEODetail citation={baseCitation} />);
        expect(screen.getByText(/Standard Unrestricted Use Data License v2\.0/)).toBeInTheDocument();
        expect(screen.getByText('(c)')).toBeInTheDocument();
    });

    it('renders the terms link', () => {
        render(<CitationPGEODetail citation={baseCitation} />);
        const link = screen.getByText('terms') as HTMLAnchorElement;
        expect(link).toBeInTheDocument();
        expect(link.href).toBe('https://example.gov.sk.ca/license');
        expect(link.target).toBe('_blank');
        expect(link.rel).toContain('noopener');
    });

    it('omits the license block entirely when no license_summary', () => {
        render(<CitationPGEODetail citation={{ ...baseCitation, license_summary: null }} />);
        expect(screen.queryByText('terms')).toBeNull();
    });
});

describe('<CitationPGEODetail /> upstream link', () => {
    it('renders "Open upstream record" link', () => {
        render(<CitationPGEODetail citation={baseCitation} />);
        const link = screen.getByText('Open upstream record') as HTMLAnchorElement;
        expect(link.href).toBe(baseCitation.source_url);
        expect(link.target).toBe('_blank');
    });

    it('hides the upstream link when source_url is missing', () => {
        render(<CitationPGEODetail citation={{ ...baseCitation, source_url: null }} />);
        expect(screen.queryByText('Open upstream record')).toBeNull();
    });
});

describe('<CitationPGEODetail /> staleness warning', () => {
    it('does NOT show the warning for fresh data (< 2 days)', () => {
        render(<CitationPGEODetail citation={{ ...baseCitation, staleness_seconds: 3600 }} />);
        expect(screen.queryByText(/Cached data:/)).toBeNull();
    });

    it('shows the warning when stale (> 2 days)', () => {
        render(<CitationPGEODetail citation={{ ...baseCitation, staleness_seconds: 86_400 * 5 }} />);
        expect(screen.getByText(/Cached data:/)).toBeInTheDocument();
    });

    it('shows the warning when very stale (> 10 days)', () => {
        render(<CitationPGEODetail citation={{ ...baseCitation, staleness_seconds: 86_400 * 30 }} />);
        expect(screen.getByText(/Cached data:/)).toBeInTheDocument();
    });
});

describe('<CitationPGEODetail /> details fetch', () => {
    const fakeSourceData: SourceData = {
        source_type: 'public_geo',
        title: 'GB Gold Showing',
        text: 'GB Gold Showing is a precious-metals occurrence in north-central SK.',
        corpus: 'public_geo',
        canonical_type: 'mineral_occurrence',
        jurisdiction: { code: 'CA-SK', name: 'Saskatchewan', authority: 'Saskatchewan Geological Survey' },
        source: { source_id: 'CA-SK-SMDI', name: 'Mineral Deposits Index', service_url: 'https://gis.saskatchewan.ca/...' },
        license: { summary: '...', url: '...' },
        refresh: { last_refreshed_at: '2026-04-15T01:00:00Z', staleness_seconds: 1985 },
        references_summary: { count: 0, documents: [] },
        entity: {
            external_id: '2661',
            name: 'GB Gold Showing',
            status: 'occurrence',
            primary_commodities: ['Au'],
            commodity_grouping: 'precious_metals',
        },
        metadata: {},
    };

    let originalFetch: typeof globalThis.fetch;

    beforeEach(() => {
        originalFetch = globalThis.fetch;
        globalThis.fetch = vi.fn().mockResolvedValue({
            ok: true,
            status: 200,
            json: async () => fakeSourceData,
        }) as any;
    });

    afterEach(() => {
        globalThis.fetch = originalFetch;
        vi.clearAllMocks();
    });

    it('shows "View details" button collapsed by default', () => {
        render(<CitationPGEODetail citation={baseCitation} />);
        expect(screen.getByText('View details')).toBeInTheDocument();
        expect(screen.queryByText(/precious-metals occurrence/)).toBeNull();
    });

    it('lazily fetches resolver on first expand', async () => {
        render(<CitationPGEODetail citation={baseCitation} />);
        expect(globalThis.fetch).not.toHaveBeenCalled();

        fireEvent.click(screen.getByText('View details'));

        await waitFor(() => {
            expect(globalThis.fetch).toHaveBeenCalledTimes(1);
        });

        const url = (globalThis.fetch as any).mock.calls[0][0];
        expect(url).toContain('/api/v1/citations/resolve');
        expect(url).toContain(encodeURIComponent(baseCitation.source_chunk_id));
        expect(url).toContain('citation_type=PGEO');
    });

    it('renders narrative text + entity fields after fetch', async () => {
        render(<CitationPGEODetail citation={baseCitation} />);
        fireEvent.click(screen.getByText('View details'));

        await waitFor(() => {
            expect(screen.getByText(/precious-metals occurrence/)).toBeInTheDocument();
        });

        // Highlighted fields per mineral_occurrence picker.
        // V1.2 schema rename: smdi_id → external_id; the user-facing
        // label is now generic "External ID" (the per-jurisdiction
        // vocabulary — SMDI for SK, MINFILE for BC — is encoded in
        // source_id which appears in the citation header).
        expect(screen.getByText('External ID')).toBeInTheDocument();
        expect(screen.getByText('2661')).toBeInTheDocument();
        expect(screen.getByText('Primary commodities')).toBeInTheDocument();
        expect(screen.getByText('Au')).toBeInTheDocument();
        expect(screen.getByText('Grouping')).toBeInTheDocument();
        expect(screen.getByText('precious_metals')).toBeInTheDocument();
    });

    it('renders the references drill-in mock with parsed canonical_type + pg_id', async () => {
        render(<CitationPGEODetail citation={baseCitation} />);
        fireEvent.click(screen.getByText('View details'));

        await waitFor(() => {
            expect(screen.getByTestId('references-drill-in')).toBeInTheDocument();
        });

        const drillin = screen.getByTestId('references-drill-in');
        expect(drillin.dataset.canonical).toBe('mineral_occurrence');
        expect(drillin.dataset.pgId).toBe('4e8cdee5-e6d4-40b4-85c0-8a25e64659e3');
        expect(drillin.dataset.count).toBe('0');
    });

    it('toggles back to collapsed on second click', async () => {
        render(<CitationPGEODetail citation={baseCitation} />);
        fireEvent.click(screen.getByText('View details'));
        await waitFor(() => expect(screen.getByText('Hide details')).toBeInTheDocument());
        fireEvent.click(screen.getByText('Hide details'));
        expect(screen.getByText('View details')).toBeInTheDocument();
    });

    it('shows error message when fetch fails', async () => {
        globalThis.fetch = vi.fn().mockResolvedValue({ ok: false, status: 500 }) as any;
        render(<CitationPGEODetail citation={baseCitation} />);
        fireEvent.click(screen.getByText('View details'));
        await waitFor(() => {
            expect(screen.getByText(/Failed to load/)).toBeInTheDocument();
        });
    });

    it('does not read auth tokens from localStorage when resolving citation', async () => {
        const getItemSpy = vi.spyOn(Storage.prototype, 'getItem');
        render(<CitationPGEODetail citation={baseCitation} />);
        fireEvent.click(screen.getByText('View details'));
        await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());

        const tokenLike = /token|jwt|secret/i;
        const offendingKeys = getItemSpy.mock.calls
            .map((call) => String(call[0]))
            .filter((key) => tokenLike.test(key));
        expect(offendingKeys).toEqual([]);
        getItemSpy.mockRestore();
    });

    it('sends the resolver fetch with same-origin credentials', async () => {
        render(<CitationPGEODetail citation={baseCitation} />);
        fireEvent.click(screen.getByText('View details'));
        await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());

        const [, init] = (globalThis.fetch as any).mock.calls[0] as [string, RequestInit];
        expect(init?.credentials).toBe('same-origin');
        const headers = (init?.headers ?? {}) as Record<string, string>;
        expect(headers.Authorization).toBeUndefined();
    });
});
