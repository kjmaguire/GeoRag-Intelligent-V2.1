/**
 * RefusalPanel.test.tsx — §10u refusal/failure panel.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import RefusalPanel, { humanizeCode } from '@/Components/RefusalPanel';

describe('humanizeCode', () => {
    it('title-cases SCREAMING_SNAKE_CASE', () => {
        expect(humanizeCode('MISSING_ASSAY_UNITS')).toBe('Missing Assay Units');
        expect(humanizeCode('TIMEOUT')).toBe('Timeout');
        expect(humanizeCode('insufficient_evidence')).toBe('Insufficient Evidence');
    });
});

describe('RefusalPanel', () => {
    it('renders nothing when message is empty', () => {
        const { container } = render(<RefusalPanel variant="failed" message="" />);
        expect(container.firstChild).toBeNull();
    });

    it('renders the fixed non-hedging headline for an insufficient_evidence refusal', () => {
        render(
            <RefusalPanel
                variant="refusal"
                message="Nothing retrieved cleared the quality floor."
                code="insufficient_evidence"
                guardCodes={['LAYER1_EMPTY']}
            />
        );
        expect(
            screen.getByText('Insufficient evidence to answer this question from the current corpus.')
        ).toBeInTheDocument();
        expect(screen.getByText('Refused — insufficient evidence')).toBeInTheDocument();
        expect(screen.getByText('Nothing retrieved cleared the quality floor.')).toBeInTheDocument();
        // The headline already says it; the "Reason:" line would repeat it.
        expect(screen.queryByText(/Reason:/)).not.toBeInTheDocument();
    });

    it('matches insufficient_evidence case-insensitively', () => {
        render(<RefusalPanel variant="refusal" message="m" code="INSUFFICIENT_EVIDENCE" />);
        expect(screen.getByText(/Insufficient evidence to answer this question/)).toBeInTheDocument();
    });

    it('uses the fixed headline when a refusal carries no code', () => {
        render(<RefusalPanel variant="refusal" message="Backend detail." />);
        expect(screen.getByText(/Insufficient evidence to answer this question/)).toBeInTheDocument();
        expect(screen.getByText('Backend detail.')).toBeInTheDocument();
        expect(screen.queryByText(/Reason:/)).not.toBeInTheDocument();
    });

    it('does not call a refusal for any other reason "insufficient evidence"', () => {
        render(
            <RefusalPanel
                variant="refusal"
                message="This question is outside what the project's data can answer."
                code="SOURCE_SCOPE_VIOLATION"
                guardCodes={['SOURCE_SCOPE_VIOLATION', 'UNSUPPORTED_QUERY_TYPE']}
            />
        );
        expect(screen.getByText('Answer withheld')).toBeInTheDocument();
        expect(screen.queryByText(/insufficient evidence/i)).not.toBeInTheDocument();
        expect(screen.getByText("This question is outside what the project's data can answer.")).toBeInTheDocument();
        expect(screen.getByText(/Source Scope Violation/)).toBeInTheDocument();
    });

    it('says "No answer produced" when the model returned nothing usable', () => {
        render(<RefusalPanel variant="refusal" message="The model returned an empty answer." code="model_no_output" />);
        expect(screen.getByText('No answer produced')).toBeInTheDocument();
        expect(screen.queryByText('Answer withheld')).not.toBeInTheDocument();
        expect(screen.queryByText(/insufficient evidence/i)).not.toBeInTheDocument();
        expect(screen.getByText('The model returned an empty answer.')).toBeInTheDocument();
        expect(screen.getByText(/Reason: Model No Output/)).toBeInTheDocument();
    });

    it('withholds neutrally when the answer was unsupported by its sources', () => {
        render(<RefusalPanel variant="refusal" message="The draft claims were not backed by the retrieved sources." code="unsupported_by_sources" />);
        expect(screen.getByText('Answer withheld')).toBeInTheDocument();
        expect(screen.queryByText(/insufficient evidence/i)).not.toBeInTheDocument();
        expect(screen.getByText(/Reason: Unsupported By Sources/)).toBeInTheDocument();
    });

    it('renders a QUERY_NOT_SEARCHABLE failed frame as a failure with its reason', () => {
        render(<RefusalPanel variant="failed" message="This question cannot be searched as written." code="QUERY_NOT_SEARCHABLE" />);
        expect(screen.getByText('Query failed')).toBeInTheDocument();
        expect(screen.getByText('This question cannot be searched as written.')).toBeInTheDocument();
        expect(screen.getByText(/Reason: Query Not Searchable/)).toBeInTheDocument();
        expect(screen.getByTestId('refusal-panel')).toHaveAttribute('data-variant', 'failed');
    });

    it('says the access check could not complete for ACCESS_CHECK_FAILED, keeping the server message as the body', () => {
        const message = 'We could not verify your access to this project right now. Please try again in a few seconds.';
        render(<RefusalPanel variant="failed" message={message} code="ACCESS_CHECK_FAILED" />);
        expect(screen.getByText('Could not check your access')).toBeInTheDocument();
        expect(screen.queryByText('Query failed')).not.toBeInTheDocument();
        expect(screen.getByText(message)).toBeInTheDocument();
    });

    it('says the service is busy for SERVICE_UNAVAILABLE, keeping the server message as the body', () => {
        const message = 'The project could not be checked right now. Please try again in a few seconds.';
        render(<RefusalPanel variant="failed" message={message} code="SERVICE_UNAVAILABLE" />);
        expect(screen.getByText('Service busy, try again')).toBeInTheDocument();
        expect(screen.queryByText('Query failed')).not.toBeInTheDocument();
        expect(screen.getByText(message)).toBeInTheDocument();
    });

    it('keeps internal guard codes out of the visible text', () => {
        render(
            <RefusalPanel
                variant="refusal"
                message="The sources disagree on this."
                code="CONFLICTING_SOURCES"
                guardCodes={['CONFLICTING_SOURCES', 'UNSUPPORTED_QUERY_TYPE']}
            />
        );
        const panel = screen.getByTestId('refusal-panel');
        expect(panel.textContent ?? '').not.toMatch(/guards:|UNSUPPORTED_QUERY_TYPE|CONFLICTING_SOURCES/);
        expect(panel.getAttribute('data-guard-codes')).toBe('CONFLICTING_SOURCES,UNSUPPORTED_QUERY_TYPE');
    });

    it('does not show the refusal headline for the failed variant', () => {
        render(<RefusalPanel variant="failed" message="Your query took too long to process." code="TIMEOUT" />);
        expect(
            screen.queryByText('Insufficient evidence to answer this question from the current corpus.')
        ).not.toBeInTheDocument();
        expect(screen.getByText('Query failed')).toBeInTheDocument();
        expect(screen.getByText('Your query took too long to process.')).toBeInTheDocument();
        expect(screen.getByText(/Timeout/)).toBeInTheDocument();
    });

    it('renders without a Reason line when code is absent', () => {
        render(<RefusalPanel variant="failed" message="Network error" />);
        expect(screen.queryByText(/Reason:/)).not.toBeInTheDocument();
    });

    it('has an alert role for accessibility', () => {
        render(<RefusalPanel variant="failed" message="boom" />);
        expect(screen.getByRole('alert')).toBeInTheDocument();
    });
});
