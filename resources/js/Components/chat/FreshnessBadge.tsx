/**
 * FreshnessBadge — B8 + V1.5-20
 *
 * Small inline pill near the confidence indicator that signals how current the
 * answer's underlying data was at query time.
 *
 * Two signals combined:
 *   1. data_version diff — `workspace_data_version_at_query` vs the current
 *      `usePage().props.workspace.data_version` (Inertia-shared by Module 8
 *      Chunk 8.5). ANY positive diff → stale, because the corpus has been
 *      updated since the answer was generated and the answer no longer
 *      reflects current state. This is the stronger signal.
 *   2. Clock age of `answered_at` — fallback when data_version is unchanged:
 *        Fresh   (<24h)    — green
 *        Recent  (24h–7d)  — amber
 *        Stale   (>7d)     — red
 *
 * A11y: aria-label describes the staleness class + reason (data_version diff
 *       or clock age) + the answered_at date.
 */

import { usePage } from '@inertiajs/react';

import { cn } from '@/lib/utils';
import type { PageProps } from '@/types';

// ── Types ─────────────────────────────────────────────────────────────────

export interface FreshnessData {
    workspace_data_version_at_query: number;
    project_data_version_at_query?: number | null;
    answered_at: string;   // ISO 8601
}

interface FreshnessBadgeProps {
    freshness: FreshnessData | null | undefined;
}

// ── Staleness helpers ─────────────────────────────────────────────────────

const ONE_DAY_MS = 24 * 60 * 60 * 1000;
const SEVEN_DAYS_MS = 7 * ONE_DAY_MS;

type StalenessClass = 'fresh' | 'recent' | 'stale';

interface StalenessResult {
    cls: StalenessClass;
    label: string;
    ariaLabel: string;
    colorClasses: string;
}

/**
 * Compute the staleness verdict.
 *
 * @param answeredAt        ISO 8601 timestamp from the answer payload.
 * @param nowMs             Optional `Date.now()` override for tests.
 * @param queryDataVersion  `workspace_data_version_at_query` from the answer
 *                          payload (the data_version the system saw when
 *                          answering). Use null/undefined to disable the
 *                          data_version diff signal.
 * @param currentDataVersion The CURRENT workspace data_version (from
 *                          Inertia's usePage().props.workspace.data_version).
 *                          Use null/undefined to disable the diff signal.
 *
 * Logic:
 *   - If both versions are present AND current > query → 'stale' (data
 *     has been updated since the answer was generated; the answer no
 *     longer reflects current state).
 *   - Otherwise fall back to clock-based age: <24h fresh, 24h-7d recent, >7d stale.
 */
export function computeStaleness(
    answeredAt: string,
    nowMs?: number,
    queryDataVersion?: number | null,
    currentDataVersion?: number | null,
): StalenessResult {
    // V1.5-20 — data_version diff is the stronger signal. Any positive diff
    // means ingestion has touched the corpus since this answer was generated,
    // so the answer is stale even if the wall clock says it's recent.
    if (
        queryDataVersion != null
        && currentDataVersion != null
        && currentDataVersion > queryDataVersion
    ) {
        const diff = currentDataVersion - queryDataVersion;
        return {
            cls: 'stale',
            label: 'Stale',
            ariaLabel:
                `Answer data is stale (workspace data_version advanced by ${diff} `
                + `since query at ${answeredAt}; corpus has been updated)`,
            colorClasses: 'text-red-400 bg-red-950/40 border-red-700/50',
        };
    }

    const queryTime = new Date(answeredAt).getTime();
    const now = nowMs ?? Date.now();
    const ageMs = now - queryTime;

    if (ageMs < ONE_DAY_MS) {
        return {
            cls: 'fresh',
            label: 'Fresh',
            ariaLabel: `Answer data is fresh (less than 24 hours old, answered ${answeredAt})`,
            colorClasses: 'text-green-400 bg-green-950/40 border-green-700/50',
        };
    }
    if (ageMs < SEVEN_DAYS_MS) {
        return {
            cls: 'recent',
            label: 'Recent',
            ariaLabel: `Answer data is recent (1–7 days old, answered ${answeredAt})`,
            colorClasses: 'text-amber-400 bg-amber-950/40 border-amber-700/50',
        };
    }
    return {
        cls: 'stale',
        label: 'Stale',
        ariaLabel: `Answer data may be stale (more than 7 days old, answered ${answeredAt})`,
        colorClasses: 'text-red-400 bg-red-950/40 border-red-700/50',
    };
}

// ── Component ─────────────────────────────────────────────────────────────

export function FreshnessBadge({ freshness }: FreshnessBadgeProps) {
    // V1.5-20 — read the live workspace.data_version from Inertia's shared
    // page props (Module 8 Chunk 8.5 wired this). Fall through to undefined
    // when the prop is absent, in which case the badge falls back to clock
    // age only — matches Module 7 Chunk 4 behaviour.
    const page = usePage<PageProps>();
    const currentDataVersion = page.props?.workspace?.data_version;

    if (!freshness || !freshness.answered_at) return null;

    const { cls, label, ariaLabel, colorClasses } = computeStaleness(
        freshness.answered_at,
        undefined,
        freshness.workspace_data_version_at_query,
        currentDataVersion,
    );

    return (
        <span
            className={cn(
                'inline-flex items-center gap-1 px-2 py-0.5 rounded-full',
                'text-[10px] font-medium border',
                colorClasses,
            )}
            aria-label={ariaLabel}
            title={ariaLabel}
            data-testid="freshness-badge"
            data-staleness={cls}
        >
            {/* Dot indicator */}
            <span
                className={cn(
                    'w-1.5 h-1.5 rounded-full shrink-0',
                    cls === 'fresh'  && 'bg-green-400',
                    cls === 'recent' && 'bg-amber-400',
                    cls === 'stale'  && 'bg-red-400',
                )}
                aria-hidden="true"
            />
            {label}
        </span>
    );
}
