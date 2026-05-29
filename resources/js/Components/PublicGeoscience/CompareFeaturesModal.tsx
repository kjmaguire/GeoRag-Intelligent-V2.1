import { useMemo } from 'react';
import { Modal } from '@/Components/Foundry/primitives';
import { useFeatureDetail, type FeatureDetail } from '@/Hooks/PublicGeoscience/useFeatureDetail';
import type { PointPopup } from '@/Components/PublicGeoscience/PublicGeoscienceMap';
import { GROUPING_COLORS, type LayerId } from './publicGeoscienceLayers';

/**
 * Compare-Features modal — Foundry-styled N-up grid showing up to 3 PG
 * features side-by-side. Cross-layer is allowed (per user pick), so the
 * grid renders shared fields up top, then per-feature unique fields.
 *
 * Each column independently fetches its full upstream record via
 * useFeatureDetail. Loading + error states are per-column so a slow
 * SMDI fetch doesn't block a fast mines fetch.
 *
 * Layout:
 *   Header           "Compare 3 features"  ✕
 *   ┌───────────┬───────────┬───────────┐
 *   │ Feature A │ Feature B │ Feature C │  ← title + eyebrow per column
 *   │ canonical │ canonical │ canonical │  ← canonical key/values
 *   │ source    │ source    │ source    │  ← upstream JSONB block
 *   │ link      │ link      │ link      │  ← upstream record link
 *   └───────────┴───────────┴───────────┘
 *
 * No diff highlighting yet — visual side-by-side is the v1 win. A v2
 * could mark fields whose values diverge across columns.
 */

interface CompareFeaturesModalProps {
    open: boolean;
    compareSet: PointPopup[];     // length 2 or 3 expected; 0/1 hides the modal at the caller
    onClose: () => void;
    onRemove: (popup: PointPopup) => void;
    onClear: () => void;
}

export default function CompareFeaturesModal({
    open,
    compareSet,
    onClose,
    onRemove,
    onClear,
}: CompareFeaturesModalProps) {
    return (
        <Modal
            open={open}
            onClose={onClose}
            maxWidth={Math.min(1200, 460 * compareSet.length + 80)}
            label="Compare features"
        >
            <div className="flex items-center justify-between gap-2 px-4 py-3 border-b" style={{ borderColor: 'var(--line-1)' }}>
                <div>
                    <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                        Compare · {compareSet.length}
                    </div>
                    <div className="text-sm font-medium" style={{ color: 'var(--fg-0)' }}>
                        Public-geoscience features side-by-side
                    </div>
                </div>
                <div className="flex items-center gap-1">
                    <button
                        type="button"
                        onClick={onClear}
                        className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border"
                        style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)', background: 'var(--bg-2)' }}
                    >
                        Clear queue
                    </button>
                    <button
                        type="button"
                        onClick={onClose}
                        aria-label="Close compare modal"
                        className="text-[10px] font-mono px-2 py-1 rounded border"
                        style={{ color: 'var(--fg-3)', borderColor: 'var(--line-2)', background: 'var(--bg-2)' }}
                    >
                        ✕
                    </button>
                </div>
            </div>

            <div
                className="grid divide-x overflow-y-auto"
                style={{
                    gridTemplateColumns: `repeat(${compareSet.length}, minmax(0, 1fr))`,
                    borderColor: 'var(--line-1)',
                    // divide-x uses borderColor on the children; we set it
                    // explicitly so the column dividers blend with the rest.
                }}
            >
                {compareSet.map((popup) => (
                    <CompareColumn
                        key={`${popup.layerId}:${idOf(popup)}`}
                        popup={popup}
                        onRemove={() => onRemove(popup)}
                    />
                ))}
            </div>
        </Modal>
    );
}

// ── One column ─────────────────────────────────────────────────────────

function CompareColumn({ popup, onRemove }: { popup: PointPopup; onRemove: () => void }) {
    const featureId = idOf(popup);
    const { data, loading, error, retry } = useFeatureDetail(popup.layerId, featureId);

    const eyebrow = eyebrowFor(popup.layerId, popup.properties);
    const title = titleFor(popup.layerId, popup.properties);
    const grouping = (popup.properties.commodity_grouping as string | undefined) || null;
    const swatch = grouping ? GROUPING_COLORS[grouping] ?? null : null;

    return (
        <div className="p-3 flex flex-col gap-3" style={{ borderColor: 'var(--line-1)' }}>
            <div className="flex items-start justify-between gap-2">
                <div className="flex items-start gap-2 min-w-0">
                    {swatch && (
                        <span
                            aria-hidden="true"
                            className="w-2 h-2 rounded-full shrink-0 mt-1"
                            style={{ background: swatch }}
                        />
                    )}
                    <div className="min-w-0">
                        <div className="text-[10px] font-mono uppercase tracking-wider truncate" style={{ color: 'var(--fg-3)' }} title={eyebrow}>
                            {eyebrow}
                        </div>
                        <div className="text-sm font-medium leading-tight" style={{ color: 'var(--fg-0)' }}>
                            {title}
                        </div>
                    </div>
                </div>
                <button
                    type="button"
                    onClick={onRemove}
                    aria-label="Remove from compare"
                    className="text-[10px] font-mono px-1.5 py-0.5 rounded border shrink-0"
                    style={{ color: 'var(--fg-3)', borderColor: 'var(--line-2)', background: 'var(--bg-2)' }}
                    title="Remove from compare"
                >
                    ✕
                </button>
            </div>

            {loading && (
                <div className="flex items-center gap-2 text-[11px] font-mono" style={{ color: 'var(--fg-3)' }}>
                    <div className="w-3 h-3 rounded-full border-2 border-t-transparent animate-spin" style={{ borderColor: 'var(--fg-3)' }} />
                    Loading…
                </div>
            )}

            {error && (
                <div className="text-[11px] font-mono px-2 py-2 rounded border" style={{ borderColor: 'var(--danger)', color: 'var(--danger)' }}>
                    {error}
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

            {data && !loading && !error && <CompareColumnBody data={data} />}
        </div>
    );
}

function CompareColumnBody({ data }: { data: FeatureDetail }) {
    const sourceAttrs = data.source_attributes ?? null;
    const reserves = data.reserves_resources ?? null;
    const sourceUrl = data.source_url as string | undefined;

    const SKIP = new Set([
        'layer', 'source_attributes', 'reserves_resources', 'source_url',
        'first_seen_at', 'created_at', 'updated_at', 'checksum', 'id',
    ]);

    const canonicalEntries = useMemo(
        () => Object.entries(data)
            .filter(([k, v]) => !SKIP.has(k) && v !== null && v !== undefined && v !== '')
            .sort(([a], [b]) => a.localeCompare(b)),
        [data],
    );

    return (
        <div className="flex flex-col gap-3 text-[11px]">
            {canonicalEntries.length > 0 && (
                <Block title="Canonical">
                    <KeyValues entries={canonicalEntries} />
                </Block>
            )}
            {reserves && Object.keys(reserves).length > 0 && (
                <Block title="Reserves / resources">
                    <KeyValues entries={Object.entries(reserves)} />
                </Block>
            )}
            {sourceAttrs && Object.keys(sourceAttrs).length > 0 && (
                <Block title="Upstream attributes">
                    <KeyValues entries={Object.entries(sourceAttrs)} mono />
                </Block>
            )}
            {sourceUrl && (
                <a
                    href={sourceUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-[10px] font-mono uppercase tracking-wider px-2 py-1.5 rounded border inline-block self-start"
                    style={{ color: 'var(--accent)', borderColor: 'var(--accent-dim)', background: 'var(--accent-bg)' }}
                >
                    Open upstream →
                </a>
            )}
        </div>
    );
}

function Block({ title, children }: { title: string; children: React.ReactNode }) {
    return (
        <section>
            <div className="text-[10px] font-mono uppercase tracking-[0.12em] mb-1" style={{ color: 'var(--fg-3)' }}>
                {title}
            </div>
            <div className="rounded border" style={{ borderColor: 'var(--line-1)', background: 'var(--bg-2)' }}>
                {children}
            </div>
        </section>
    );
}

function KeyValues({
    entries,
    mono = false,
}: {
    entries: Array<[string, unknown]>;
    mono?: boolean;
}) {
    return (
        <div className="grid grid-cols-[110px_1fr] gap-x-2 gap-y-1 px-2 py-1.5">
            {entries.map(([key, value]) => (
                <div key={key} className="contents">
                    <span className="text-[10px] truncate" style={{ color: 'var(--fg-3)' }} title={key}>
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

// ── Helpers (small dupes from ExpandedFeatureCard — kept inline to
//    avoid premature extraction; promote to a shared module if a third
//    consumer appears). ─────────────────────────────────────────────────

function idOf(popup: PointPopup): string {
    return String(
        popup.properties.source_feature_id
        ?? popup.properties.feature_id
        ?? popup.properties.smdi
        ?? popup.properties.drillhole_id
        ?? popup.properties.id
        ?? '',
    );
}

function humanise(key: string): string {
    const lower = key.toLowerCase();
    return lower.charAt(0).toUpperCase() + lower.slice(1).replace(/_/g, ' ');
}

function formatValue(value: unknown): string {
    if (value === null || value === undefined) return '—';
    if (typeof value === 'boolean') return value ? 'Yes' : 'No';
    if (typeof value === 'number') return String(value);
    if (typeof value === 'string') return value;
    if (Array.isArray(value)) return value.length === 0 ? '—' : value.map(String).join(', ');
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
