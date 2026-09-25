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

    it('renders the fixed non-hedging headline for the refusal variant', () => {
        render(
            <RefusalPanel
                variant="refusal"
                message="Terminal repair strategy triggered: REFUSE_OUT_OF_SCOPE."
                code="SOURCE_SCOPE_VIOLATION"
                guardCodes={['SOURCE_SCOPE_VIOLATION', 'UNSUPPORTED_QUERY_TYPE']}
            />
        );
        expect(
            screen.getByText('Insufficient evidence to answer this question from the current corpus.')
        ).toBeInTheDocument();
        expect(screen.getByText(/Terminal repair strategy triggered/)).toBeInTheDocument();
        expect(screen.getByText(/Source Scope Violation/)).toBeInTheDocument();
        expect(screen.getByText(/guards: SOURCE_SCOPE_VIOLATION, UNSUPPORTED_QUERY_TYPE/)).toBeInTheDocument();
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
