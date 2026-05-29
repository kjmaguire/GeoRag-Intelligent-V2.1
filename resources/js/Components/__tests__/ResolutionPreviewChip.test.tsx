// @ts-nocheck
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import ResolutionPreviewChip from '@/Components/ResolutionPreviewChip';

describe('ResolutionPreviewChip', () => {
    it('renders nothing when resolution is null', () => {
        const { container } = render(<ResolutionPreviewChip resolution={null} />);
        expect(container.firstChild).toBeNull();
    });

    it('renders nothing when original === rewritten (no real rewrite)', () => {
        const { container } = render(
            <ResolutionPreviewChip
                resolution={{
                    original_query: 'what is the deepest hole?',
                    rewritten_query: 'what is the deepest hole?',
                    overall_confidence: 1.0,
                }}
            />
        );
        expect(container.firstChild).toBeNull();
    });

    it('renders "Interpreted as" with the rewritten form', () => {
        render(
            <ResolutionPreviewChip
                resolution={{
                    original_query: 'what are ITS top assays?',
                    rewritten_query: "what are PLS-22-08's top assays?",
                    overall_confidence: 0.85,
                }}
            />
        );
        expect(screen.getByText('Interpreted as')).toBeInTheDocument();
        expect(screen.getByText("what are PLS-22-08's top assays?")).toBeInTheDocument();
        expect(screen.getByText(/from your message: what are ITS top assays\?/)).toBeInTheDocument();
    });

    it('shows high confidence chip when ≥ 0.85', () => {
        render(
            <ResolutionPreviewChip
                resolution={{
                    original_query: 'a',
                    rewritten_query: 'b',
                    overall_confidence: 0.9,
                }}
            />
        );
        expect(screen.getByText('high')).toBeInTheDocument();
    });

    it('shows medium confidence chip in 0.6–0.85 range', () => {
        render(
            <ResolutionPreviewChip
                resolution={{
                    original_query: 'a',
                    rewritten_query: 'b',
                    overall_confidence: 0.7,
                }}
            />
        );
        expect(screen.getByText('medium')).toBeInTheDocument();
    });

    it('shows low confidence chip when < 0.6', () => {
        render(
            <ResolutionPreviewChip
                resolution={{
                    original_query: 'a',
                    rewritten_query: 'b',
                    overall_confidence: 0.3,
                }}
            />
        );
        expect(screen.getByText('low')).toBeInTheDocument();
    });

    it('omits tone chip when confidence is missing', () => {
        render(
            <ResolutionPreviewChip
                resolution={{
                    original_query: 'a',
                    rewritten_query: 'b',
                }}
            />
        );
        expect(screen.queryByText('high')).not.toBeInTheDocument();
        expect(screen.queryByText('medium')).not.toBeInTheDocument();
        expect(screen.queryByText('low')).not.toBeInTheDocument();
    });
});
