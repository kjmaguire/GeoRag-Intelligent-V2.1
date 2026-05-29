// @ts-nocheck
/**
 * DataQualityBadge.test.tsx — CC-02 Item 3.
 *
 * Pins the visual-treatment rules for the lithology data-quality
 * surface shown in the DrillholeDetail sticky header.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { DataQualityBadge } from '@/Components/Foundry/DataQualityBadge';

describe('DataQualityBadge', () => {
    it('renders nothing when total is 0', () => {
        const { container } = render(
            <DataQualityBadge counters={{ exact: 0, fuzzy: 0, unmapped: 0, total: 0 }} />
        );
        expect(container.firstChild).toBeNull();
    });

    it('shows "all exact" state when every interval matched the catalogue', () => {
        render(
            <DataQualityBadge counters={{ exact: 47, fuzzy: 0, unmapped: 0, total: 47 }} />
        );
        expect(screen.getByText(/Lithology:/i)).toBeInTheDocument();
        expect(screen.getByText(/47 \/ 47 exact/i)).toBeInTheDocument();
    });

    it('shows fuzzy count when some intervals were fuzzy-matched', () => {
        render(
            <DataQualityBadge counters={{ exact: 40, fuzzy: 7, unmapped: 0, total: 47 }} />
        );
        expect(screen.getByText(/7 \/ 47 fuzzy/i)).toBeInTheDocument();
    });

    it('shows unmapped count when some intervals have no rock_code', () => {
        render(
            <DataQualityBadge counters={{ exact: 30, fuzzy: 5, unmapped: 12, total: 47 }} />
        );
        expect(screen.getByText(/12 \/ 47 unmapped/i)).toBeInTheDocument();
    });

    it('renders as an anchor when href is provided', () => {
        render(
            <DataQualityBadge
                counters={{ exact: 47, fuzzy: 0, unmapped: 0, total: 47 }}
                href="/projects/x/ingest-quality?hole=PLS-22-08"
            />
        );
        const link = screen.getByRole('link');
        expect(link.getAttribute('href')).toBe('/projects/x/ingest-quality?hole=PLS-22-08');
    });

    it('renders as a span (no anchor) when href is omitted', () => {
        render(
            <DataQualityBadge counters={{ exact: 47, fuzzy: 0, unmapped: 0, total: 47 }} />
        );
        expect(screen.queryByRole('link')).toBeNull();
    });

    it('aria-label includes all counter breakdown for screen-reader users', () => {
        render(
            <DataQualityBadge counters={{ exact: 30, fuzzy: 5, unmapped: 12, total: 47 }} />
        );
        const label = screen.getByLabelText(/30 exact/);
        expect(label).toBeInTheDocument();
        expect(label.getAttribute('aria-label')).toMatch(/5 fuzzy/);
        expect(label.getAttribute('aria-label')).toMatch(/12 unmapped/);
        expect(label.getAttribute('aria-label')).toMatch(/47 total/);
    });

    it('honours a custom label override', () => {
        render(
            <DataQualityBadge
                counters={{ exact: 47, fuzzy: 0, unmapped: 0, total: 47 }}
                label="Custom override"
            />
        );
        expect(screen.getByText(/Custom override/)).toBeInTheDocument();
    });
});
