import * as React from 'react';
import { Pill } from '@/Components/Foundry/primitives';

/**
 * DataQualityBadge — CC-02 Item 3.
 *
 * Surfaces silver-data review state to geologists in the Foundry
 * answer/data paths. Today's Foundry already surfaces answer-side
 * lifecycle (draft → generated → validated → committed → rejected) via
 * ChatMessage.tsx; the gap closed here is *data-side* review state —
 * specifically lithology rock_code resolution quality from the
 * silver.lithology table introduced in CC-02 Item 1.
 *
 * Buckets:
 *   - exact          — rock_code_confidence = 1.0 (catalogue hit)
 *   - fuzzy          — 0 < rock_code_confidence < 1.0 (rapidfuzz)
 *   - unmapped       — rock_code IS NULL (catalogue gap)
 *
 * Tone is picked from the worst bucket present:
 *   - any unmapped         → 'warn'
 *   - any fuzzy (no unmapped) → 'info'
 *   - all exact            → 'accent'
 *
 * Clicking the badge follows the supplied href (typically a deep link
 * into the lithology review page or the IngestQuality dashboard scoped
 * to this hole / project). When href is omitted the badge renders as a
 * plain pill — useful for read-only contexts.
 */

export interface DataQualityBadgeProps {
    counters: {
        exact: number;
        fuzzy: number;
        unmapped: number;
        total: number;
    };
    /** Optional click-through. When set, renders as an <a>. */
    href?: string;
    /** Override the label shown in the pill — default is auto-derived. */
    label?: string;
    /** Optional className passed to the outer wrapper. */
    className?: string;
}

function pickTone(counters: DataQualityBadgeProps['counters']): 'accent' | 'info' | 'warn' {
    if (counters.unmapped > 0) return 'warn';
    if (counters.fuzzy > 0) return 'info';
    return 'accent';
}

function defaultLabel(counters: DataQualityBadgeProps['counters']): string {
    const { exact, fuzzy, unmapped, total } = counters;
    if (total === 0) return 'No lithology';
    if (unmapped > 0) {
        return `${unmapped} / ${total} unmapped`;
    }
    if (fuzzy > 0) {
        return `${fuzzy} / ${total} fuzzy`;
    }
    return `${total} / ${total} exact`;
}

function tooltipText(counters: DataQualityBadgeProps['counters']): string {
    const parts: string[] = [
        `${counters.exact} exact catalogue match${counters.exact === 1 ? '' : 'es'}`,
        `${counters.fuzzy} fuzzy match${counters.fuzzy === 1 ? '' : 'es'} (needs review)`,
        `${counters.unmapped} unmapped (catalogue gap)`,
        `${counters.total} total interval${counters.total === 1 ? '' : 's'}`,
    ];
    return parts.join(' · ');
}

export function DataQualityBadge({
    counters,
    href,
    label,
    className = '',
}: DataQualityBadgeProps) {
    if (counters.total === 0) {
        return null;
    }

    const tone = pickTone(counters);
    const displayLabel = label ?? defaultLabel(counters);
    const tip = tooltipText(counters);

    const pillEl = (
        <Pill tone={tone} dot>
            Lithology: {displayLabel}
        </Pill>
    );

    if (!href) {
        return (
            <span className={className} title={tip} aria-label={`Lithology data quality — ${tip}`}>
                {pillEl}
            </span>
        );
    }

    return (
        <a
            href={href}
            className={['inline-flex', className].join(' ').trim()}
            title={tip}
            aria-label={`Lithology data quality — ${tip}. Click to review.`}
        >
            {pillEl}
        </a>
    );
}

export default DataQualityBadge;
