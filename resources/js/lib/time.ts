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

/* ------------------------------------------------------------------ *
 * Timestamp display — the ONE place server timestamps become UI text.
 *
 * Server timestamps arrive as ISO strings that are UTC but frequently
 * WITHOUT a timezone designator ("2026-08-14T09:31:22" or
 * "2026-08-14 09:31:22"). JavaScript's Date parses those as LOCAL time,
 * so slicing (`created_at.slice(11,16)`) or naive `new Date(...)` both
 * rendered UTC wall-clock digits as if they were local — factually wrong
 * for any user west or east of Greenwich. These helpers parse as UTC,
 * then format in the viewer's local zone.
 * ------------------------------------------------------------------ */

/**
 * Parse a server timestamp as UTC. Strings without a timezone designator
 * get "Z" appended; a bare 2-digit offset (Postgres's abbreviated whole-
 * hour form, e.g. "+00" for UTC) gets padded to the 4-digit form ("+00:00")
 * that the JS Date constructor actually accepts -- "+00" alone parses as
 * Invalid Date. A space separator is normalized to "T". Returns null for
 * unparseable input.
 *
 * Bug fix (2026-08-15, live-browser-observed): chat message timestamps
 * rendered as an em dash on every page reload. Root cause: Postgres
 * timestamptz text output for UTC is "2026-08-15 19:49:41+00" (2-digit
 * offset), but the old zone-detection regex required a 4-digit offset
 * ("+00:00"), so it treated "+00" as "no zone" and appended another "Z" --
 * "...+00Z" is unparseable, so every reloaded/history timestamp silently
 * fell back to the "--" placeholder. Reproduced via GET /chat?thread=...
 * and confirmed against the raw DB value (`timestamp with time zone`
 * column, "+00" text form).
 */
export function parseUtc(value: string | null | undefined): Date | null {
    if (!value) return null;
    let s = value.trim();
    if (s.length === 0) return null;
    // "YYYY-MM-DD HH:MM…" → ISO 'T' separator.
    if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(s)) s = s.replace(' ', 'T');
    if (/T\d{2}:\d{2}/.test(s)) {
        if (/[+-]\d{2}$/.test(s)) {
            // Bare 2-digit offset ("+00", "-05") -- pad to the 4-digit form.
            s += ':00';
        } else if (!/(Z|[+-]\d{2}:?\d{2})$/i.test(s)) {
            // No zone designator at all -- server timestamps are UTC.
            s += 'Z';
        }
    }
    const d = new Date(s);
    return Number.isNaN(d.getTime()) ? null : d;
}

function pad2(n: number): string {
    return String(n).padStart(2, '0');
}

/**
 * Full "when" label: relative for the recent past ("just now", "12m ago",
 * "2h ago"), absolute local "YYYY-MM-DD HH:MM" beyond 24 h. Date-only
 * inputs ("1979-05-12") pass through unchanged — they carry no time
 * component to convert and UTC-parsing them can shift the calendar day.
 * Null/empty/unparseable input renders as an em dash / the raw string.
 */
export function formatWhen(value: string | null | undefined): string {
    if (!value) return '—';
    if (/^\d{4}-\d{2}-\d{2}$/.test(value.trim())) return value.trim();
    const d = parseUtc(value);
    if (!d) return value;
    const ageSec = (Date.now() - d.getTime()) / 1000;
    if (ageSec >= 0 && ageSec < 60) return 'just now';
    if (ageSec >= 60 && ageSec < 3600) return `${Math.floor(ageSec / 60)}m ago`;
    if (ageSec >= 3600 && ageSec < 86_400) return `${Math.floor(ageSec / 3600)}h ago`;
    // Absolute, in the viewer's local zone, mono-friendly.
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

/**
 * Local wall-clock "HH:MM" for a server timestamp. Use where only the
 * time-of-day matters (chat message meta, "refreshed at" lines).
 */
export function formatTime(value: string | null | undefined): string {
    const d = parseUtc(value);
    if (!d) return '—';
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
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
