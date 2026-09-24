/**
 * EvidenceInspector.test.tsx — §10s evidence inspector Sheet.
 *
 * Reuses the existing `GET /api/v1/citations/resolve` contract (the same
 * one `Pages/Foundry/Chat.tsx` already calls inline) — see the component
 * docblock for why this route was chosen over the unwired
 * `GET /api/v1/evidence/{evidence_id}`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import EvidenceInspector from '@/Components/EvidenceInspector';

const citation = {
    citation_id: 'cit-1',
    citation_type: 'NI43',
    source_chunk_id: 'georag_reports:report-1:section=7:chunk=abc',
    document_title: 'NI 43-101 Technical Report',
    relevance_score: 0.87,
};

describe('EvidenceInspector', () => {
    let originalFetch: typeof globalThis.fetch;
    let fetchMock: ReturnType<typeof vi.fn>;

    beforeEach(() => {
        originalFetch = globalThis.fetch;
        fetchMock = vi.fn();
        globalThis.fetch = fetchMock as unknown as typeof fetch;
    });

    afterEach(() => {
        globalThis.fetch = originalFetch;
        vi.clearAllMocks();
    });

    it('renders nothing (closed Sheet) when open is false', () => {
        render(
            <EvidenceInspector citation={null} open={false} onOpenChange={() => {}} projectSlug="demo" />
        );
        expect(screen.queryByTestId('evidence-inspector')).not.toBeInTheDocument();
        expect(fetchMock).not.toHaveBeenCalled();
    });

    it('fetches /api/v1/citations/resolve when opened with a citation', async () => {
        fetchMock.mockResolvedValue({
            ok: true,
            status: 200,
            json: async () => ({
                text: 'The deposit hosts a roll-front uranium mineralisation style.',
                source_type: 'report',
                title: 'NI 43-101 Technical Report',
                section_title: 'Section 7',
                section_number: '7',
                metadata: { company: 'Acme Uranium', filing_date: '2024-03-15', report_id: 'report-1' },
            }),
        });

        render(
            <EvidenceInspector citation={citation} open onOpenChange={() => {}} projectSlug="demo" />
        );

        expect(fetchMock).toHaveBeenCalledWith(
            expect.stringContaining('/api/v1/citations/resolve?source_chunk_id='),
            expect.any(Object)
        );

        await waitFor(() =>
            expect(screen.getByTestId('evidence-inspector-text')).toHaveTextContent(
                'The deposit hosts a roll-front uranium mineralisation style.'
            )
        );
        expect(screen.getByText('NI 43-101 Technical Report')).toBeInTheDocument();
        expect(screen.getByText('Acme Uranium')).toBeInTheDocument();
        expect(screen.getByText('Open in Reader →')).toHaveAttribute(
            'href',
            '/projects/demo/reports/report-1?section=7'
        );
    });

    it('shows a loading state before the fetch resolves', () => {
        fetchMock.mockReturnValue(new Promise(() => {})); // never resolves
        render(
            <EvidenceInspector citation={citation} open onOpenChange={() => {}} projectSlug="demo" />
        );
        expect(screen.getByTestId('evidence-inspector-loading')).toBeInTheDocument();
    });

    it('shows an error state when the fetch fails', async () => {
        fetchMock.mockResolvedValue({ ok: false, status: 500 });
        render(
            <EvidenceInspector citation={citation} open onOpenChange={() => {}} projectSlug="demo" />
        );
        await waitFor(() => expect(screen.getByTestId('evidence-inspector-error')).toBeInTheDocument());
    });

    it('calls onReportIssue with the citation when the feedback hook button is clicked', async () => {
        fetchMock.mockResolvedValue({
            ok: true,
            status: 200,
            json: async () => ({ text: 'Some text', source_type: 'report' }),
        });
        const onReportIssue = vi.fn();
        render(
            <EvidenceInspector
                citation={citation}
                open
                onOpenChange={() => {}}
                projectSlug="demo"
                onReportIssue={onReportIssue}
            />
        );
        await waitFor(() => expect(screen.getByTestId('evidence-inspector-text')).toBeInTheDocument());
        fireEvent.click(screen.getByText('👎 Report citation issue'));
        expect(onReportIssue).toHaveBeenCalledWith(citation);
    });
});
