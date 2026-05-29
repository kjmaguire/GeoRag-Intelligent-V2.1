// @ts-nocheck
/**
 * CoverageTableCard.test.tsx
 *
 * Coverage:
 *   - Empty state when rows missing
 *   - Ingest-stage gap header renders processed/indexed/gap_pct
 *   - One row per attribute with progress bar (role=progressbar, aria-valuenow)
 *   - Rows are sorted ascending by coverage_pct (lowest coverage first =
 *     biggest gap surfaced at the top)
 *   - Optional `notes` render under the attribute name
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import CoverageTableCard, {
    type CoverageRow,
    type IngestGap,
} from '../CoverageTableCard';

const ROWS: CoverageRow[] = [
    { attribute: 'Assays', collars_with_data: 320, collars_total: 567, coverage_pct: 56.4 },
    { attribute: 'Lithology', collars_with_data: 540, collars_total: 567, coverage_pct: 95.2 },
    {
        attribute: 'Structure',
        collars_with_data: 0,
        collars_total: 567,
        coverage_pct: 0.0,
        notes: 'no extractor wired',
    },
    { attribute: 'Alteration', collars_with_data: 88, collars_total: 567, coverage_pct: 15.5 },
    { attribute: 'Geophysics', collars_with_data: 250, collars_total: 567, coverage_pct: 44.1 },
];

const INGEST_GAP: IngestGap = {
    indexed: 39744,
    processed: 1209,
    gap_pct: 96.96,
};

describe('CoverageTableCard — empty guard', () => {
    it('renders an empty state when rows is empty', () => {
        render(<CoverageTableCard rows={[]} ingestGap={INGEST_GAP} />);
        expect(screen.getByTestId('coverage-empty')).toBeDefined();
    });
});

describe('CoverageTableCard — ingest-stage gap header', () => {
    it('renders processed and indexed counts with thousands separators', () => {
        render(<CoverageTableCard rows={ROWS} ingestGap={INGEST_GAP} />);
        const header = screen.getByTestId('coverage-ingest-gap');
        expect(header.textContent).toContain('1,209');
        expect(header.textContent).toContain('39,744');
        expect(header.textContent).toContain('97.0% gap');
    });

    it('omits the ingest-gap header when not provided', () => {
        render(<CoverageTableCard rows={ROWS} ingestGap={null} />);
        expect(screen.queryByTestId('coverage-ingest-gap')).toBeNull();
    });
});

describe('CoverageTableCard — attribute rows', () => {
    it('renders one row per attribute', () => {
        render(<CoverageTableCard rows={ROWS} ingestGap={INGEST_GAP} />);
        ROWS.forEach((row) => {
            expect(screen.getByTestId(`coverage-row-${row.attribute}`)).toBeDefined();
        });
    });

    it('renders a progressbar with aria-valuenow per row', () => {
        render(<CoverageTableCard rows={ROWS} ingestGap={INGEST_GAP} />);
        const bars = document.querySelectorAll('[role="progressbar"]');
        expect(bars.length).toBe(ROWS.length);
        const assayBar = screen
            .getByTestId('coverage-row-Assays')
            .querySelector('[role="progressbar"]');
        expect(assayBar?.getAttribute('aria-valuenow')).toBe('56.4');
    });

    it('renders the notes line when provided', () => {
        render(<CoverageTableCard rows={ROWS} ingestGap={INGEST_GAP} />);
        expect(screen.getByText('no extractor wired')).toBeDefined();
    });

    it('sorts rows ascending by coverage_pct (worst gap first)', () => {
        const { container } = render(<CoverageTableCard rows={ROWS} ingestGap={INGEST_GAP} />);
        const rows = container.querySelectorAll('[data-testid^="coverage-row-"]');
        const order = Array.from(rows).map((r) => r.getAttribute('data-testid'));
        expect(order).toEqual([
            'coverage-row-Structure',
            'coverage-row-Alteration',
            'coverage-row-Geophysics',
            'coverage-row-Assays',
            'coverage-row-Lithology',
        ]);
    });
});
