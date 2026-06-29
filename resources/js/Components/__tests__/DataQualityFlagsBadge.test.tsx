/**
 * DataQualityFlagsBadge.test.tsx — Plan §6a badge UI.
 *
 * Renders the three-dot indicator + expandable flag list. Covers
 * the visibility rule (hidden on zero flags), per-severity counts,
 * popover toggle, and flag-row rendering.
 */

import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import DataQualityFlagsBadge, {
    type DataQualityFlag,
    type DataQualityFlagsBadgeData,
} from '../DataQualityFlagsBadge';


function _flag(overrides: Partial<DataQualityFlag> = {}): DataQualityFlag {
    return {
        flag_id: 'f-1',
        flag_type: 'collar.missing_elevation',
        severity: 'WARNING',
        description: 'Collar ECK-22-001: elevation is NULL. Back-calc from PDF.',
        rule_id: 'collar.missing_elevation',
        rule_version: 'v1.0',
        flagged_at: '2026-05-29T01:00:00Z',
        ...overrides,
    };
}


function _data(overrides: Partial<DataQualityFlagsBadgeData> = {}): DataQualityFlagsBadgeData {
    return {
        counts: { ERROR: 0, WARNING: 1, INFO: 2 },
        open_total: 3,
        flags: [
            _flag(),
            _flag({ flag_id: 'f-2', flag_type: 'collar.missing_dip', severity: 'INFO' }),
            _flag({ flag_id: 'f-3', flag_type: 'collar.missing_azimuth', severity: 'INFO' }),
        ],
        ...overrides,
    };
}


// ---------------------------------------------------------------------------
// Visibility
// ---------------------------------------------------------------------------


describe('DataQualityFlagsBadge — visibility', () => {
    it('renders nothing when data is null', () => {
        const { container } = render(<DataQualityFlagsBadge data={null} />);
        expect(container.firstChild).toBeNull();
    });

    it('renders nothing when data is undefined', () => {
        const { container } = render(<DataQualityFlagsBadge />);
        expect(container.firstChild).toBeNull();
    });

    it('renders nothing when open_total is 0', () => {
        const { container } = render(<DataQualityFlagsBadge data={_data({
            counts: { ERROR: 0, WARNING: 0, INFO: 0 },
            open_total: 0,
            flags: [],
        })} />);
        expect(container.firstChild).toBeNull();
    });
});


// ---------------------------------------------------------------------------
// Severity dot counts
// ---------------------------------------------------------------------------


describe('DataQualityFlagsBadge — severity counts', () => {
    it('shows counts for each non-zero severity', () => {
        render(<DataQualityFlagsBadge data={_data({
            counts: { ERROR: 1, WARNING: 2, INFO: 3 },
            open_total: 6,
        })} />);
        // The numbers should be visible somewhere in the badge text.
        const button = screen.getByRole('button');
        expect(button.textContent).toContain('1');  // ERROR
        expect(button.textContent).toContain('2');  // WARNING
        expect(button.textContent).toContain('3');  // INFO
    });

    it('omits a severity dot when its count is 0', () => {
        const { container } = render(<DataQualityFlagsBadge data={_data({
            counts: { ERROR: 0, WARNING: 1, INFO: 0 },
            open_total: 1,
        })} />);
        // Only WARNING dot rendered → only one dot.
        const dots = container.querySelectorAll('.rounded-full');
        expect(dots.length).toBe(1);
    });

    it('label defaults to "Quality"', () => {
        render(<DataQualityFlagsBadge data={_data()} />);
        expect(screen.getByText('Quality')).toBeTruthy();
    });

    it('label override propagates', () => {
        render(<DataQualityFlagsBadge data={_data()} label="DQ" />);
        expect(screen.getByText('DQ')).toBeTruthy();
    });
});


// ---------------------------------------------------------------------------
// Popover toggle
// ---------------------------------------------------------------------------


describe('DataQualityFlagsBadge — popover', () => {
    it('flag list is hidden by default', () => {
        render(<DataQualityFlagsBadge data={_data()} />);
        // Each flag's description shouldn't be in the DOM yet.
        expect(screen.queryByText(/elevation is NULL/)).toBeNull();
    });

    it('clicking the badge expands the flag list', () => {
        render(<DataQualityFlagsBadge data={_data()} />);
        fireEvent.click(screen.getByRole('button'));
        // Now the descriptions are visible. Fixture has 3 flags with
        // the same description text so use getAllByText.
        expect(screen.getAllByText(/elevation is NULL/).length).toBeGreaterThan(0);
    });

    it('clicking the badge again collapses', () => {
        render(<DataQualityFlagsBadge data={_data()} />);
        const button = screen.getByRole('button');
        fireEvent.click(button);
        expect(screen.getAllByText(/elevation is NULL/).length).toBeGreaterThan(0);
        fireEvent.click(button);
        expect(screen.queryAllByText(/elevation is NULL/).length).toBe(0);
    });

    it('expanded popover shows all flag descriptions', () => {
        render(<DataQualityFlagsBadge data={_data()} />);
        fireEvent.click(screen.getByRole('button'));
        // 3 fixture flags, all with the same description text.
        const descs = screen.getAllByText(/elevation is NULL/);
        expect(descs.length).toBe(3);
    });

    it('expanded popover shows the total flag count', () => {
        render(<DataQualityFlagsBadge data={_data()} />);
        fireEvent.click(screen.getByRole('button'));
        expect(screen.getByText(/3 open flag/)).toBeTruthy();
    });

    it('expanded popover renders rule_version when present', () => {
        render(<DataQualityFlagsBadge data={_data()} />);
        fireEvent.click(screen.getByRole('button'));
        expect(screen.getAllByText(/v1\.0/).length).toBeGreaterThan(0);
    });
});
