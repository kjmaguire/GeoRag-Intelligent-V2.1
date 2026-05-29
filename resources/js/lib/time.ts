/**
 * Time / age formatting helpers.
 *
 * Used for the Public Geoscience citation card's staleness badge —
 * "last refreshed 33 min ago" style labels — and anywhere else a
 * consistent time-ago string is useful.
 *
 * Deliberately string-in, string-out: the function takes a seconds
 * integer and returns a label, with a parallel staleness-level indicator
 * (`fresh` / `stale` / `very_stale`) so the caller can choose a color
 * without duplicating the threshold logic.
 */

export type StalenessLevel = 'fresh' | 'stale' | 'very_stale';

export interface StalenessInfo {
    /** Short label suitable for inline display: "just now", "3 min ago", "2 days ago". */
    label: string;
    /** Longer form for tooltip / aria-label: "refreshed 2 days ago". */
    long_label: string;
    /** Coarse level the UI uses to pick a color. */
    level: StalenessLevel;
}

// Thresholds are deliberately per-hour/day, not configured per-workspace.
// The kickoff locks these as V1 defaults; "measured, not planned" lets us
// tune once real upstream refresh cadence data is in hand.
const FRESH_SECONDS = 86_400 * 2;        // ≤ 2 days = fresh
const STALE_SECONDS = 86_400 * 10;       // ≤ 10 days = stale, > = very_stale

/**
 * Format a staleness age in seconds as a short human-readable label.
 *
 * Negative / NaN / null inputs are coerced to "unknown"; callers don't
 * need to guard. The label intentionally matches git/GitHub conventions
 * ("3 min ago", "2 days ago") so geologists aren't surprised by novel
 * formats.
 */
export function formatStaleness(seconds: number | null | undefined): StalenessInfo {
    if (seconds == null || !Number.isFinite(seconds) || seconds < 0) {
        return {
            label: 'unknown',
            long_label: 'refresh age unknown',
            level: 'very_stale',
        };
    }

    const level: StalenessLevel =
        seconds <= FRESH_SECONDS
            ? 'fresh'
            : seconds <= STALE_SECONDS
                ? 'stale'
                : 'very_stale';

    const label = _humanize(seconds);
    return {
        label,
        long_label: label === 'just now' ? 'refreshed just now' : `refreshed ${label}`,
        level,
    };
}

function _humanize(seconds: number): string {
    if (seconds < 60) return 'just now';
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.round(seconds / 3600);
    if (hours < 24) return `${hours} ${hours === 1 ? 'hour' : 'hours'} ago`;
    const days = Math.round(seconds / 86_400);
    if (days < 30) return `${days} ${days === 1 ? 'day' : 'days'} ago`;
    const months = Math.round(seconds / (86_400 * 30));
    if (months < 12) return `${months} ${months === 1 ? 'month' : 'months'} ago`;
    const years = Math.round(seconds / (86_400 * 365));
    return `${years} ${years === 1 ? 'year' : 'years'} ago`;
}
