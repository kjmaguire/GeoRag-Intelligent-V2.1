import { describe, it, expect } from 'vitest';
import { formatStaleness } from '../time';

describe('formatStaleness', () => {
    describe('edge cases', () => {
        it('returns "unknown" for null', () => {
            const r = formatStaleness(null);
            expect(r.label).toBe('unknown');
            expect(r.long_label).toBe('refresh age unknown');
            expect(r.level).toBe('very_stale');
        });

        it('returns "unknown" for undefined', () => {
            const r = formatStaleness(undefined);
            expect(r.label).toBe('unknown');
        });

        it('returns "unknown" for NaN', () => {
            expect(formatStaleness(Number.NaN).label).toBe('unknown');
        });

        it('returns "unknown" for negative seconds', () => {
            expect(formatStaleness(-5).label).toBe('unknown');
        });

        it('returns "unknown" for Infinity', () => {
            expect(formatStaleness(Infinity).label).toBe('unknown');
        });
    });

    describe('humanized labels', () => {
        it('< 60 seconds = "just now"', () => {
            expect(formatStaleness(0).label).toBe('just now');
            expect(formatStaleness(59).label).toBe('just now');
        });

        it('60–3540 seconds = "N min ago" (rounded)', () => {
            expect(formatStaleness(60).label).toBe('1 min ago');
            expect(formatStaleness(120).label).toBe('2 min ago');
            expect(formatStaleness(1985).label).toBe('33 min ago'); // matches the live response
            // 3540s = 59 min. At ≥ 1800s the hours-rounding kicks in
            // (Math.round(3599/3600) === 1), so values that round to
            // 60 min should display as 1 hour instead.
            expect(formatStaleness(3540).label).toBe('59 min ago');
            expect(formatStaleness(3599).label).toBe('1 hour ago');
        });

        it('1–23 hours = "N hour(s) ago"', () => {
            expect(formatStaleness(3600).label).toBe('1 hour ago');
            expect(formatStaleness(7200).label).toBe('2 hours ago');
            expect(formatStaleness(82800).label).toBe('23 hours ago');
        });

        it('1–29 days = "N day(s) ago"', () => {
            expect(formatStaleness(86_400).label).toBe('1 day ago');
            expect(formatStaleness(86_400 * 5).label).toBe('5 days ago');
        });

        it('1–11 months = "N month(s) ago"', () => {
            expect(formatStaleness(86_400 * 60).label).toBe('2 months ago');
            expect(formatStaleness(86_400 * 30 * 11).label).toBe('11 months ago');
        });

        it('1+ years = "N year(s) ago"', () => {
            expect(formatStaleness(86_400 * 365).label).toBe('1 year ago');
            expect(formatStaleness(86_400 * 365 * 3).label).toBe('3 years ago');
        });
    });

    describe('long_label', () => {
        it('prefixes "refreshed" except for "just now"', () => {
            expect(formatStaleness(30).long_label).toBe('refreshed just now');
            expect(formatStaleness(120).long_label).toBe('refreshed 2 min ago');
            expect(formatStaleness(86_400).long_label).toBe('refreshed 1 day ago');
        });
    });

    describe('staleness level (color thresholds)', () => {
        it('≤ 2 days = "fresh"', () => {
            expect(formatStaleness(0).level).toBe('fresh');
            expect(formatStaleness(86_400).level).toBe('fresh');
            expect(formatStaleness(86_400 * 2).level).toBe('fresh');
        });

        it('2 days < age ≤ 10 days = "stale"', () => {
            expect(formatStaleness(86_400 * 2 + 1).level).toBe('stale');
            expect(formatStaleness(86_400 * 5).level).toBe('stale');
            expect(formatStaleness(86_400 * 10).level).toBe('stale');
        });

        it('> 10 days = "very_stale"', () => {
            expect(formatStaleness(86_400 * 10 + 1).level).toBe('very_stale');
            expect(formatStaleness(86_400 * 365).level).toBe('very_stale');
        });
    });
});
