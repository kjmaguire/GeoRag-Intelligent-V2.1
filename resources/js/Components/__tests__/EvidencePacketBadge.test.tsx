/**
 * EvidencePacketBadge.test.tsx — plan §3a/§3b typed-evidence summary strip.
 *
 * Pins the visual rules:
 *   - null packet → nothing renders
 *   - empty evidence list → nothing renders
 *   - kind chips appear with counts in known-kind authority order
 *   - no budget pill and no "Graph paths" label
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
        // component should still render document -> collar -> spatial.
        render(
            <EvidencePacketBadge
                packet={{
                    evidence: [
                        { kind: 'spatial' },
                        { kind: 'collar' },
                        { kind: 'document' },
                    ],
                    remaining_budget: 1000,
                }}
            />
        );
        const labelTexts = screen
            .getAllByText(/Documents|Collars|Spatial/)
            .map((el) => el.textContent ?? '')
            .filter((t) => ['Documents', 'Collars', 'Spatial'].includes(t));
        expect(labelTexts).toEqual(['Documents', 'Collars', 'Spatial']);
    });

    it('never shows the context-window budget, even when the packet carries one', () => {
        render(
            <EvidencePacketBadge
                packet={{
                    evidence: [{ kind: 'document' }],
                    remaining_budget: 4200,
                }}
            />
        );
        expect(screen.getByText('Documents')).toBeInTheDocument();
        expect(screen.queryByText(/budget/i)).not.toBeInTheDocument();
        expect(screen.queryByText('4200')).not.toBeInTheDocument();
    });

    it('does not label anything "Graph paths" (no graph store exists)', () => {
        const { container } = render(
            <EvidencePacketBadge
                packet={{
                    evidence: [{ kind: 'document' }, { kind: 'graph' }],
                }}
            />
        );
        expect(container.textContent ?? '').not.toMatch(/graph paths/i);
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
