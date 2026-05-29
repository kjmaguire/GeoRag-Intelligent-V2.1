// @ts-nocheck
/**
 * ConflictCards.test.tsx
 *
 * Tests for the B8 ConflictCards component.
 * Spec: Global Invariant 7 — never auto-pick a winner.
 *
 * Coverage:
 *   - No render when conflicts is null
 *   - No render when conflicts is empty array
 *   - Renders N side-by-side panels for N-value conflict
 *   - Each evidence_id is a clickable chip
 *   - Clicking opens inspector (mock callback)
 *   - Aria roles present (region, figure, figcaption)
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ConflictCards } from '../chat/ConflictCards';
import type { ConflictEntry } from '../chat/ConflictCards';

// ── Helpers ───────────────────────────────────────────────────────────────

function makeConflict(overrides: Partial<ConflictEntry> = {}): ConflictEntry {
    return {
        entity_key: 'DH-001',
        property_name: 'total_depth',
        evidence_ids: ['ev-aaa-111', 'ev-bbb-222'],
        values: ['450.5m', '451.2m'],
        ...overrides,
    };
}

function renderCards(conflicts, onInspect = vi.fn()) {
    return render(
        <ConflictCards conflicts={conflicts} onInspectEvidence={onInspect} />
    );
}

// ── Null / empty guard ────────────────────────────────────────────────────

describe('ConflictCards — null/empty guard', () => {
    it('renders nothing when conflicts is null', () => {
        const { container } = renderCards(null);
        expect(container.firstChild).toBeNull();
    });

    it('renders nothing when conflicts is undefined', () => {
        const { container } = renderCards(undefined);
        expect(container.firstChild).toBeNull();
    });

    it('renders nothing when conflicts is an empty array', () => {
        const { container } = renderCards([]);
        expect(container.firstChild).toBeNull();
    });
});

// ── Side-by-side panels ───────────────────────────────────────────────────

describe('ConflictCards — side-by-side value panels', () => {
    it('renders the warning heading', () => {
        renderCards([makeConflict()]);
        expect(
            screen.getByText(/conflicting evidence detected/i)
        ).toBeDefined();
    });

    it('renders two value panels for a 2-value conflict', () => {
        renderCards([makeConflict()]);
        // Should show "Value 1" and "Value 2" labels
        expect(screen.getByText('Value 1')).toBeDefined();
        expect(screen.getByText('Value 2')).toBeDefined();
        expect(screen.getByText('450.5m')).toBeDefined();
        expect(screen.getByText('451.2m')).toBeDefined();
    });

    it('renders entity_key and property_name in the figcaption', () => {
        renderCards([makeConflict({ entity_key: 'DH-XYZ', property_name: 'azimuth' })]);
        expect(screen.getByText('DH-XYZ')).toBeDefined();
        expect(screen.getByText('azimuth')).toBeDefined();
    });

    it('renders two cards for two separate conflict entries', () => {
        const conflicts = [
            makeConflict({ entity_key: 'DH-001', property_name: 'depth' }),
            makeConflict({ entity_key: 'DH-002', property_name: 'grade' }),
        ];
        renderCards(conflicts);
        expect(screen.getByText('DH-001')).toBeDefined();
        expect(screen.getByText('DH-002')).toBeDefined();
    });
});

// ── Evidence chips ────────────────────────────────────────────────────────

describe('ConflictCards — evidence chips', () => {
    it('renders a clickable chip for each evidence_id', () => {
        renderCards([makeConflict()]);
        const chip1 = screen.getByTestId('evidence-chip-ev-aaa-111');
        const chip2 = screen.getByTestId('evidence-chip-ev-bbb-222');
        expect(chip1).toBeDefined();
        expect(chip2).toBeDefined();
    });

    it('calls onInspectEvidence with the evidence_id when a chip is clicked', () => {
        const onInspect = vi.fn();
        renderCards([makeConflict()], onInspect);

        const chip = screen.getByTestId('evidence-chip-ev-aaa-111');
        fireEvent.click(chip);

        expect(onInspect).toHaveBeenCalledWith('ev-aaa-111');
    });

    it('calls onInspectEvidence with the correct evidence_id for the second chip', () => {
        const onInspect = vi.fn();
        renderCards([makeConflict()], onInspect);

        fireEvent.click(screen.getByTestId('evidence-chip-ev-bbb-222'));
        expect(onInspect).toHaveBeenCalledWith('ev-bbb-222');
    });
});

// ── Aria roles ────────────────────────────────────────────────────────────

describe('ConflictCards — aria roles', () => {
    it('renders a section with role="region"', () => {
        renderCards([makeConflict()]);
        const region = document.querySelector('[role="region"]');
        expect(region).toBeTruthy();
    });

    it('region is aria-labelledby the heading', () => {
        renderCards([makeConflict()]);
        const region = document.querySelector('[role="region"]');
        const headingId = region?.getAttribute('aria-labelledby');
        expect(headingId).toBeTruthy();
        expect(document.getElementById(headingId)).toBeTruthy();
    });

    it('each conflict is wrapped in a <figure>', () => {
        renderCards([makeConflict()]);
        expect(document.querySelector('figure')).toBeTruthy();
    });

    it('each figure has a <figcaption>', () => {
        renderCards([makeConflict()]);
        expect(document.querySelector('figcaption')).toBeTruthy();
    });
});
