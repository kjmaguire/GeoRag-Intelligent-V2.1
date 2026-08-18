import { describe, expect, it } from 'vitest';
import { formatU3O8Pct } from '../grade';

/**
 * Regression guard for a 100x grade overstatement.
 *
 * CompareHolesModal formatted mean_u3o8_pct as `(value * 100).toFixed(3)`
 * while WorkspaceMap formatted the same field as `value.toFixed(3)`. The
 * field is already a percentage (derive_intervals.py: ORE at `grade > 0.02`,
 * documented "GRADE > 0.02 %eU3O8"), so the compare view rendered a 0.35%
 * U₃O₈ intercept as "35.000%".
 */
describe('formatU3O8Pct', () => {
    it('renders a percent value without rescaling it', () => {
        // 0.35 means 0.35% U₃O₈ — NOT 35%.
        expect(formatU3O8Pct(0.35)).toBe('0.350%');
    });

    it('keeps the ore cutoff readable at three decimals', () => {
        // derive_intervals.py classifies ORE above 0.02 %eU3O8; that cutoff
        // must not round away to "0.0%".
        expect(formatU3O8Pct(0.02)).toBe('0.020%');
    });

    it('does not inflate high-grade intercepts', () => {
        // Athabasca high-grade. Rescaling would render an impossible 1800%.
        expect(formatU3O8Pct(18)).toBe('18.000%');
    });

    it('returns the placeholder for null and undefined', () => {
        expect(formatU3O8Pct(null)).toBe('—');
        expect(formatU3O8Pct(undefined)).toBe('—');
    });

    it('returns the placeholder for NaN rather than "NaN%"', () => {
        expect(formatU3O8Pct(Number.NaN)).toBe('—');
    });

    it('honours a custom placeholder', () => {
        expect(formatU3O8Pct(null, 'n/a')).toBe('n/a');
    });

    it('renders zero as a real value, not the placeholder', () => {
        expect(formatU3O8Pct(0)).toBe('0.000%');
    });
});
