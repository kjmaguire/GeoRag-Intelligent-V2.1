import { useFeatureDetail, type FeatureDetail } from '@/Hooks/PublicGeoscience/useFeatureDetail';
import type { PointPopup } from '@/Components/PublicGeoscience/PublicGeoscienceMap';
import { GROUPING_COLORS, type LayerId } from './publicGeoscienceLayers';

/**
 * Expanded feature card — pops out of the compact FeaturePopupCard when
 * the user clicks "Expand →". Anchored top-left of the map (same corner
 * as the small popup), but wider + taller so it can show the full
 * upstream record fetched from the new
 * /api/v1/public-geoscience/features/{layer}/{id} endpoint.
 *
 * Sections (rendered only when the underlying data is non-empty):
 *   - Header: title + eyebrow + commodity swatch + Collapse/Compare/Close
 *   - Canonical fields: the per-layer set the small popup also shows,
 *     laid out as a 2-col grid for scanability
 *   - Reserves/resources: parsed JSONB if present
 *   - Upstream source attributes: raw JSONB blob from the source agency
 *     (SMDI ARC tile properties, MINFILE field exports, etc.) shown as
 *     a flat key/value table so geologists can see what the upstream
 *     record actually said before we canonicalised it.
 *   - Upstream record link: same as the compact popup
 *
 * Loading state is a small inline spinner; error state shows the HTTP
 * status + a retry button.
 */

interface ExpandedFeatureCardProps {
    popup: PointPopup;
    isInCompareSet: boolean;
    compareFull: boolean;     // disable + Compare when queue already at max
    onClose: () => void;
    onCollapse: () => void;   // back to the compact popup
    onCompareToggle: () => void;
}

export default function ExpandedFeatureCard({
    popup,
    isInCompareSet,
    compareFull,
    onClose,
    onCollapse,
    onCompareToggle,
}: ExpandedFeatureCardProps) {
    const featureId = String(
        popup.properties.source_feature_id
        ?? popup.properties.feature_id
        ?? popup.properties.smdi
        ?? popup.properties.drillhole_id
        ?? popup.properties.id
        ?? '',
    );
    const { data, loading, error, retry } = useFeatureDetail(popup.layerId, featureId);

    const eyebrow = eyebrowFor(popup.layerId, popup.properties);
    const title = titleFor(popup.layerId, popup.properties);
    const grouping = (popup.properties.commodity_grouping as string | undefined) || null;
    const swatch = grouping ? GROUPING_COLORS[grouping] ?? null : null;

    return (
        <div
            className="absolute top-2 left-2 z-20 rounded border flex flex-col"
            style={{
                background: 'var(--bg-1)',
                borderColor: 'var(--line-2)',
                color: 'var(--fg-1)',
                width: 'min(520px, calc(100% - 16px))',
                maxHeight: 'calc(100% - 16px)',
            }}
            data-pg-popup="expanded"
            role="dialog"
            aria-label="Expanded feature details"
        >
            {/* Header — sticky so the user can scroll the body without losing it */}
            <div
                className="flex items-start justify-between gap-2 px-3 py-2 border-b shrink-0"
                style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}
            >
                <div className="flex items-start gap-2 min-w-0">
                    {swatch && (
                        <span
                            aria-hidden="true"
                            className="w-2 h-2 rounded-full shrink-0 mt-1"
                            style={{ background: swatch }}
                        />
                    )}
                    <div className="min-w-0">
                        <div className="text-[10px] font-mono uppercase tracking-wider truncate" style={{ color: 'var(--fg-3)' }}>
                            {eyebrow}
                        </div>
                        <div className="text-sm font-medium leading-tight" style={{ color: 'var(--fg-0)' }}>
                            {title}
                        </div>
                    </div>
                </div>
                <div className="flex items-center gap-1 shrink-0">
                    <button
                        type="button"
                        onClick={onCompareToggle}
                        disabled={!isInCompareSet && compareFull}
                        className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border disabled:opacity-40"
                        title={
                            isInCompareSet ? 'Remove from compare queue'
                            : compareFull ? 'Compare queue is full (3 max)'
                            : 'Add to compare queue'
                        }
                        style={{
                            color: isInCompareSet ? '#e8a36b' : 'var(--fg-2)',
                            borderColor: isInCompareSet ? '#e8a36b' : 'var(--line-2)',
                            background: isInCompareSet ? 'rgba(232,163,107,0.12)' : 'var(--bg-2)',
                        }}
                    >
                        {isInCompareSet ? '✓ Compare' : '+ Compare'}
                    </button>
                    <button
                        type="button"
                        onClick={onCollapse}
                        className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border"
                        title="Collapse to compact popup"
                        style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)', background: 'var(--bg-2)' }}
                    >
                        Collapse
                    </button>
                    <button
                        type="button"
                        onClick={onClose}
                        aria-label="Close feature details"
                        className="text-[10px] font-mono px-2 py-1 rounded border"
                        style={{ color: 'var(--fg-3)', borderColor: 'var(--line-2)', background: 'var(--bg-2)' }}
                    >
                        ✕
                    </button>
                </div>
            </div>

            {/* Body — scrolls if the upstream record is long */}
            <div className="px-3 py-3 overflow-y-auto" style={{ maxHeight: 'calc(100vh - 200px)' }}>
                {loading && (
                    <div className="flex items-center gap-2 text-[11px] font-mono" style={{ color: 'var(--fg-3)' }}>
                        <div className="w-3 h-3 rounded-full border-2 border-t-transparent animate-spin" style={{ borderColor: 'var(--fg-3)' }} />
                        Loading upstream record…
                    </div>
                )}

                {error && (
                    <div className="text-[11px] font-mono px-2 py-2 rounded border" style={{ borderColor: 'var(--danger)', color: 'var(--danger)', background: 'color-mix(in oklch, var(--danger) 8%, transparent)' }}>
                        Failed to load upstream record: {error}
                        <button
                            type="button"
                            onClick={retry}
                            className="ml-2 underline"
                            style={{ color: 'var(--accent)' }}
                        >
                            Retry
                        </button>
                    </div>
                )}

                {data && !loading && !error && <FeatureDetailBody data={data} />}
            </div>
        </div>
    );
}

// ── Body sections ──────────────────────────────────────────────────────

function FeatureDetailBody({ data }: { data: FeatureDetail }) {
    const sourceAttrs = data.source_attributes ?? null;
    const reserves = data.reserves_resources ?? null;
    const sourceUrl = data.source_url as string | undefined;

    // Build the "canonical fields" view: every column except the JSONB
    // blobs (rendered separately) and a small internal-fields skip list.
    const SKIP = new Set([
        'layer', 'source_attributes', 'reserves_resources', 'source_url',
        'first_seen_at', 'created_at', 'updated_at', 'checksum',
        'id', // internal UUID — not interesting upstream
    ]);
    const canonicalEntries = Object.entries(data)
        .filter(([k, v]) => !SKIP.has(k) && v !== null && v !== undefined && v !== '')
        .sort(([a], [b]) => a.localeCompare(b));

    return (
        <div className="flex flex-col gap-4 text-[11px]">
            {canonicalEntries.length > 0 && (
                <Section title="Canonical record">
                    <KeyValueGrid entries={canonicalEntries} />
                </Section>
            )}

            {reserves && Object.keys(reserves).length > 0 && (
                <Section title="Reserves & resources">
                    <KeyValueGrid entries={Object.entries(reserves)} />
                </Section>
            )}

            {sourceAttrs && Object.keys(sourceAttrs).length > 0 && (
                <Section
                    title="Upstream source attributes"
                    hint="Raw fields as the source agency exposed them — pre-canonicalisation."
                >
                    <KeyValueGrid entries={Object.entries(sourceAttrs)} mono />
                </Section>
            )}

            {sourceUrl && (
                <div className="pt-1">
                    <a
                        href={sourceUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-[10px] font-mono uppercase tracking-wider px-2 py-1.5 rounded border inline-block"
                        style={{ color: 'var(--accent)', borderColor: 'var(--accent-dim)', background: 'var(--accent-bg)' }}
                    >
                        Open upstream record →
                    </a>
                </div>
            )}
        </div>
    );
}

function Section({
    title,
    hint,
    children,
}: {
    title: string;
    hint?: string;
    children: React.ReactNode;
}) {
    return (
        <section>
            <div className="text-[10px] font-mono uppercase tracking-[0.12em] mb-1.5" style={{ color: 'var(--fg-3)' }}>
                {title}
            </div>
            {hint && (
                <div className="text-[10px] mb-1.5 leading-snug" style={{ color: 'var(--fg-3)' }}>
                    {hint}
                </div>
            )}
            <div className="rounded border" style={{ borderColor: 'var(--line-1)', background: 'var(--bg-2)' }}>
                {children}
            </div>
        </section>
    );
}

function KeyValueGrid({
    entries,
    mono = false,
}: {
    entries: Array<[string, unknown]>;
    mono?: boolean;
}) {
    return (
        <div className="grid grid-cols-[140px_1fr] gap-x-3 gap-y-1 px-2 py-1.5">
            {entries.map(([key, value]) => (
                <div key={key} className="contents">
                    <span className="text-[11px] truncate" style={{ color: 'var(--fg-3)' }} title={key}>
                        {humanise(key)}
                    </span>
                    <span
                        className={mono ? 'text-[11px] font-mono break-words' : 'text-[11px] break-words'}
                        style={{ color: 'var(--fg-1)' }}
                    >
                        {formatValue(value)}
                    </span>
                </div>
            ))}
        </div>
    );
}

// ── Formatters ─────────────────────────────────────────────────────────

function humanise(key: string): string {
    // 'source_feature_id' → 'Source feature id'; 'PRIMARYCOMMODITIES' →
    // 'Primarycommodities'. Lossy but good enough for a debug surface.
    const lower = key.toLowerCase();
    return lower.charAt(0).toUpperCase() + lower.slice(1).replace(/_/g, ' ');
}

function formatValue(value: unknown): string {
    if (value === null || value === undefined) return '—';
    if (typeof value === 'boolean') return value ? 'Yes' : 'No';
    if (typeof value === 'number') return String(value);
    if (typeof value === 'string') return value;
    if (Array.isArray(value)) return value.length === 0 ? '—' : value.map(String).join(', ');
    // Object / nested JSON — pretty-print compactly.
    try {
        return JSON.stringify(value, null, 2);
    } catch {
        return String(value);
    }
}

function eyebrowFor(layerId: LayerId, props: Record<string, any>): string {
    const juris = props.jurisdiction_code as string | undefined;
    const label: Partial<Record<LayerId, string>> = {
        pg_mines: 'Mine',
        pg_mineral_occurrences: 'Mineral Occurrence',
        pg_drillhole_collars: 'Drillhole Collar',
        pg_resource_potential: 'Resource Potential Zone',
        pg_rock_samples: 'Rock Sample',
        pg_assessment_surveys: 'Assessment Survey',
    };
    const resolved = label[layerId] ?? 'Public geoscience';
    return juris ? `${resolved} · ${juris}` : resolved;
}

function titleFor(layerId: LayerId, props: Record<string, any>): string {
    switch (layerId) {
        case 'pg_mines':
            return props.name || 'Unnamed mine';
        case 'pg_mineral_occurrences': {
            const idLabel = props.jurisdiction_code === 'CA-BC'
                ? 'MINFILE'
                : props.jurisdiction_code === 'CA-SK'
                    ? 'SMDI'
                    : 'ID';
            return props.name || `${idLabel} ${props.external_id ?? '—'}`;
        }
        case 'pg_drillhole_collars':
            return props.drillhole_name || props.drillhole_id || 'Drillhole';
        case 'pg_resource_potential':
            return `${titleCase(props.commodity)} — rank ${props.potential_rank ?? '—'}`;
        case 'pg_rock_samples':
            return (
                props.sample_number
                || props.station
                || (props.report_number ? `Sample in ${props.report_number}` : 'Rock sample')
            );
        case 'pg_assessment_surveys':
            return (
                props.survey_type === 'airborne' ? 'Airborne survey'
                : props.survey_type === 'ground' ? 'Ground survey'
                : props.survey_type === 'underground' ? 'Underground survey'
                : 'Assessment survey'
            );
        default:
            return String(props.name || props.source_feature_id || 'Public geoscience feature');
    }
}

function titleCase(value: unknown): string {
    if (!value) return '—';
    const s = String(value).trim();
    if (!s) return '—';
    return s[0].toUpperCase() + s.slice(1).toLowerCase();
}
