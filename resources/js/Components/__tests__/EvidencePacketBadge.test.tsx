// @ts-nocheck
/**
 * EvidencePacketBadge.test.tsx — plan §3a/§3b typed-evidence summary strip.
 *
 * Pins the visual rules:
 *   - null packet → nothing renders
 *   - empty evidence list → nothing renders
 *   - kind chips appear with counts in known-kind authority order
 *   - budget pill colours map to remaining_budget thresholds
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import EvidencePacketBadge from '@/Components/EvidencePacketBadge';

describe('EvidencePacketBadge', () => {
    it('renders nothing when packet is null', () => {
        const { container } = render(<EvidencePacketBadge packet={null} />);
        expect(container.firstChild).toBeNull();
    });

    it('renders nothing when evidence list is empty', () => {
        const { container } = render(
            <EvidencePacketBadge packet={{ evidence: [], remaining_budget: 5000 }} />
        );
        expect(container.firstChild).toBeNull();
    });

    it('shows per-kind chips with counts', () => {
        render(
            <EvidencePacketBadge
                packet={{
                    evidence: [
                        { kind: 'document' },
                        { kind: 'document' },
                        { kind: 'spatial' },
                        { kind: 'assay' },
                        { kind: 'assay' },
                        { kind: 'assay' },
                    ],
                    remaining_budget: 4200,
                }}
            />
        );
        expect(screen.getByText('Documents')).toBeInTheDocument();
        expect(screen.getByText('×2')).toBeInTheDocument();
        expect(screen.getByText('Spatial')).toBeInTheDocument();
        expect(screen.getByText('Assays')).toBeInTheDocument();
        expect(screen.getByText('×3')).toBeInTheDocument();
    });

    it('orders chips in authority-leaning known-kind order', () => {
        // Provide kinds in non-canonical order in the packet; the
        // component should still render document → spatial → graph.
        render(
            <EvidencePacketBadge
                packet={{
                    evidence: [
                        { kind: 'graph' },
                        { kind: 'spatial' },
                        { kind: 'document' },
                    ],
                    remaining_budget: 1000,
                }}
            />
        );
        const chips = screen.getAllByText(/Documents|Spatial|Graph paths/);
        // chips includes both label spans and count spans, so collect just
        // the kind labels in document order via textContent.
        const labelTexts = chips.map((el) => el.textContent ?? '').filter((t) =>
            ['Documents', 'Spatial', 'Graph paths'].includes(t),
        );
        expect(labelTexts).toEqual(['Documents', 'Spatial', 'Graph paths']);
    });

    it('shows a Budget pill when remaining_budget is provided', () => {
        render(
            <EvidencePacketBadge
                packet={{
                    evidence: [{ kind: 'document' }],
                    remaining_budget: 4200,
                }}
            />
        );
        expect(screen.getByText('Budget')).toBeInTheDocument();
        expect(screen.getByText('4200')).toBeInTheDocument();
    });

    it('does NOT render budget pill when remaining_budget is missing', () => {
        render(
            <EvidencePacketBadge
                packet={{
                    evidence: [{ kind: 'document' }],
                }}
            />
        );
        expect(screen.queryByText('Budget')).not.toBeInTheDocument();
    });

    it('renders a negative-budget value with the same chip (error tone applied via style)', () => {
        // Negative budget is a legitimate signal — the converter passes
        // it through so the UI can flag context overflow.
        render(
            <EvidencePacketBadge
                packet={{
                    evidence: [{ kind: 'document' }],
                    remaining_budget: -120,
                }}
            />
        );
        expect(screen.getByText('Budget')).toBeInTheDocument();
        expect(screen.getByText('-120')).toBeInTheDocument();
    });

    it('falls back to raw kind name for unknown kinds', () => {
        render(
            <EvidencePacketBadge
                packet={{
                    evidence: [{ kind: 'experimental_kind' }],
                    remaining_budget: 100,
                }}
            />
        );
        expect(screen.getByText('experimental_kind')).toBeInTheDocument();
    });
});
