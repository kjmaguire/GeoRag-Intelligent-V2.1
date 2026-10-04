/**
 * EvidenceInspector.test.tsx — §10s evidence inspector Sheet.
 *
 * Reuses the existing `GET /api/v1/citations/resolve` contract (the same
 * one `Pages/Foundry/Chat.tsx` already calls inline) — see the component
 * docblock for why this route was chosen over the unwired
 * `GET /api/v1/evidence/{evidence_id}`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent, cleanup } from '@testing-library/react';
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

    afterEach(async () => {
        // Unmount the Sheet here, then let one macrotask run. Radix's
        // FocusScope dispatches its unmount focus event from a
        // setTimeout(0); left pending, it can fire after the file's jsdom
        // environment is torn down and fail the run with "parameter 1 is
        // not of type 'Event'" (CI, 2026-09-29) even though every test
        // passed.
        cleanup();
        await new Promise((resolve) => setTimeout(resolve, 0));
        globalThis.fetch = originalFetch;
        vi.clearAllMocks();
    });

    it('renders nothing (closed Sheet) when open is false', () => {
        render(<EvidenceInspector citation={null} open={false} onOpenChange={() => {}} projectSlug="demo" />);
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
                metadata: {
                    company: 'Acme Uranium',
                    filing_date: '2024-03-15T00:00:00Z',
                    report_id: 'report-1',
                    commodity: 'U',
                },
            }),
        });

        render(<EvidenceInspector citation={citation} open onOpenChange={() => {}} projectSlug="demo" />);

        expect(fetchMock).toHaveBeenCalledWith(
            expect.stringContaining('/api/v1/citations/resolve?source_chunk_id='),
            expect.any(Object),
        );

        await waitFor(() =>
            expect(screen.getByTestId('evidence-inspector-text')).toHaveTextContent(
                'The deposit hosts a roll-front uranium mineralisation style.',
            ),
        );
        expect(screen.getByText('NI 43-101 Technical Report')).toBeInTheDocument();
        expect(screen.getByText('Report date')).toBeInTheDocument();
        expect(screen.getByText('2024-03-15')).toBeInTheDocument();
        // Not on the allow-list: raw bag keys and ids stay out of the panel.
        expect(screen.queryByText('Acme Uranium')).not.toBeInTheDocument();
        expect(screen.queryByText('Report ID')).not.toBeInTheDocument();
        expect(screen.queryByText('report-1')).not.toBeInTheDocument();
        expect(screen.getByText('Open in Reader →')).toHaveAttribute(
            'href',
            '/projects/demo/reports/report-1?section=7',
        );
    });

    it('shows hole, depth interval and confidence from a structured source and hides ids and lab fields', async () => {
        fetchMock.mockResolvedValue({
            ok: true,
            status: 200,
            json: async () => ({
                text: 'U3O8 0.31 % over 1.5 m',
                source_type: 'assays',
                title: 'Assay PLS-22-08',
                metadata: {
                    assay_id: 'a-9',
                    collar_id: 'c-1',
                    hole_id: 'PLS-22-08',
                    sample_id: 's-4',
                    from_depth: 101.5,
                    to_depth: '103',
                    lab_name: 'SRC',
                    certificate_ref: 'CERT-77',
                    confidence: 0.82,
                    page: 14,
                },
            }),
        });
        render(<EvidenceInspector citation={citation} open onOpenChange={() => {}} projectSlug="demo" />);

        await waitFor(() => expect(screen.getByTestId('evidence-inspector-text')).toBeInTheDocument());
        expect(screen.getByText('PLS-22-08')).toBeInTheDocument();
        expect(screen.getByText('101.5–103 m')).toBeInTheDocument();
        expect(screen.getByText('82%')).toBeInTheDocument();
        expect(screen.getByText('14')).toBeInTheDocument();
        for (const hidden of ['a-9', 'c-1', 's-4', 'SRC', 'CERT-77']) {
            expect(screen.queryByText(hidden)).not.toBeInTheDocument();
        }
    });

    it('shows a loading state before the fetch resolves', () => {
        fetchMock.mockReturnValue(new Promise(() => {})); // never resolves
        render(<EvidenceInspector citation={citation} open onOpenChange={() => {}} projectSlug="demo" />);
        expect(screen.getByTestId('evidence-inspector-loading')).toBeInTheDocument();
    });

    it('shows an error state when the fetch fails', async () => {
        fetchMock.mockResolvedValue({ ok: false, status: 500 });
        render(<EvidenceInspector citation={citation} open onOpenChange={() => {}} projectSlug="demo" />);
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
                answerRunId="run-1"
                onReportIssue={onReportIssue}
            />,
        );
        await waitFor(() => expect(screen.getByTestId('evidence-inspector-text')).toBeInTheDocument());
        fireEvent.click(screen.getByText('👎 Report citation issue'));
        expect(onReportIssue).toHaveBeenCalledWith(citation);
    });

    it.each([null, undefined])(
        'does not offer "Report citation issue" without an answer run (%s)',
        async (answerRunId) => {
            fetchMock.mockResolvedValue({
                ok: true,
                status: 200,
                json: async () => ({ text: 'Some text', source_type: 'report' }),
            });
            render(
                <EvidenceInspector
                    citation={citation}
                    open
                    onOpenChange={() => {}}
                    projectSlug="demo"
                    answerRunId={answerRunId}
                    onReportIssue={vi.fn()}
                />,
            );
            await waitFor(() => expect(screen.getByTestId('evidence-inspector-text')).toBeInTheDocument());
            expect(screen.queryByText('👎 Report citation issue')).not.toBeInTheDocument();
        },
    );
});
