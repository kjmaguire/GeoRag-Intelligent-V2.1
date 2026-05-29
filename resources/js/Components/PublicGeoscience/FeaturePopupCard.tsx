import type { PointPopup } from '@/Components/PublicGeoscience/PublicGeoscienceMap';
import { GROUPING_COLORS, type LayerId } from './publicGeoscienceLayers';

/**
 * Feature detail card — shown TOP-LEFT of the map when a user clicks a
 * mine / occurrence / drillhole / resource potential zone / rock sample /
 * assessment survey.
 *
 * Rendered INLINE (not portalled) so it anchors inside the map's relative
 * wrapper, matching the WorkspaceMap "Hole" detail panel placement. The
 * parent map section must be `position: relative` (it is — see
 * Pages/PublicGeoscience/Index.tsx's Card content wrapper).
 *
 * Visual design mirrors Foundry/WorkspaceMap activeHole panel:
 *   - Foundry tokens (var(--bg-1) / var(--line-2) / var(--fg-*))
 *   - Compact min-w 240 / max-w 280
 *   - font-mono eyebrow + sm title + 2-col KV grid
 *   - Bottom "Open upstream record →" link in accent color
 *
 * Per-layer field rendering preserves the field selection a geologist
 * actually cares about per feature type.
 *
 * MVT feature properties arrive as JSON-encoded TEXT[] for array columns
 * (commodities, primary_commodities, etc.), so we parse lazily.
 */

interface FeaturePopupCardProps {
    popup: PointPopup;
    onClose: () => void;
    /** Called when the user clicks "Expand →". Parent renders ExpandedFeatureCard. */
    onExpand?: () => void;
    /** Called when the user clicks "+ Compare" / "✓ Compare" to toggle queue membership. */
    onCompareToggle?: () => void;
    /** True if the current feature is already in the compare queue. */
    isInCompareSet?: boolean;
    /** True if the compare queue is full and the user can't add more. */
    compareFull?: boolean;
}

export default function FeaturePopupCard({
    popup,
    onClose,
    onExpand,
    onCompareToggle,
    isInCompareSet = false,
    compareFull = false,
}: FeaturePopupCardProps) {
    const { layerId, properties } = popup;
    const title = titleFor(layerId, properties);
    const eyebrow = eyebrowFor(layerId, properties);
    const grouping = (properties.commodity_grouping as string | undefined) || null;
    const swatch = grouping ? GROUPING_COLORS[grouping] ?? null : null;

    return (
        <div
            className="absolute top-2 left-2 z-10 px-3 py-2 rounded border min-w-[240px] max-w-[280px]"
            style={{ background: 'var(--bg-1)', borderColor: 'var(--line-2)', color: 'var(--fg-1)' }}
            data-pg-popup="true"
            role="dialog"
            aria-label="Feature details"
        >
            <div className="flex items-start justify-between gap-2">
                <div className="flex items-center gap-2 min-w-0">
                    {swatch && (
                        <span
                            aria-hidden="true"
                            className="w-2 h-2 rounded-full shrink-0"
                            style={{ background: swatch }}
                        />
                    )}
                    <div className="text-[11px] font-mono uppercase tracking-wider truncate" style={{ color: 'var(--fg-3)' }}>
                        {eyebrow}
                    </div>
                </div>
                <button
                    type="button"
                    onClick={onClose}
                    aria-label="Close feature details"
                    className="text-[10px] font-mono"
                    style={{ color: 'var(--fg-3)' }}
                >
                    ✕
                </button>
            </div>
            <div className="text-sm font-medium mt-0.5 leading-tight" style={{ color: 'var(--fg-0)' }}>
                {title}
            </div>
            <div className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[11px]">
                <FeatureBody layerId={layerId} properties={properties} />
            </div>
            <SourceFooter properties={properties} />
            {(onExpand || onCompareToggle) && (
                <div className="mt-2 pt-2 border-t flex items-center gap-1" style={{ borderColor: 'var(--line-1)' }}>
                    {onExpand && (
                        <button
                            type="button"
                            onClick={onExpand}
                            className="flex-1 text-[10px] font-mono uppercase tracking-wider px-2 py-1.5 rounded border"
                            style={{ color: 'var(--accent)', borderColor: 'var(--accent-dim)', background: 'var(--accent-bg)' }}
                            title="Show the full upstream record in a larger panel"
                        >
                            Expand →
                        </button>
                    )}
                    {onCompareToggle && (
                        <button
                            type="button"
                            onClick={onCompareToggle}
                            disabled={!isInCompareSet && compareFull}
                            className="text-[10px] font-mono uppercase tracking-wider px-2 py-1.5 rounded border disabled:opacity-40"
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
                    )}
                </div>
            )}
        </div>
    );
}

// ── Per-layer field rendering ───────────────────────────────────────────

function FeatureBody({
    layerId,
    properties,
}: {
    layerId: LayerId;
    properties: Record<string, any>;
}) {
    switch (layerId) {
        case 'pg_mines':
            return <MineFields properties={properties} />;
        case 'pg_mineral_occurrences':
            return <MineralOccurrenceFields properties={properties} />;
        case 'pg_drillhole_collars':
            return <DrillholeFields properties={properties} />;
        case 'pg_resource_potential':
            return <ResourcePotentialFields properties={properties} />;
        case 'pg_rock_samples':
            return <RockSampleFields properties={properties} />;
        case 'pg_assessment_surveys':
            return <AssessmentSurveyFields properties={properties} />;
        default:
            // Tier 2+3 layers exist in the LayerId union as reserved but have
            // no LAYER_SPECS entry yet, so they can't be clicked in the UI.
            // Render a minimal fallback so a future accidental dispatch
            // doesn't produce a blank popup silently.
            return <GenericFields properties={properties} />;
    }
}

function MineFields({ properties }: { properties: Record<string, any> }) {
    const commodities = parseList(properties.commodities);
    return (
        <>
            <KV label="Status" value={properties.status} mono />
            <KV label="Commodities" value={commodities.join(', ')} />
            <KV label="Operator" value={properties.operator} />
        </>
    );
}

function MineralOccurrenceFields({ properties }: { properties: Record<string, any> }) {
    const primary = parseList(properties.primary_commodities);
    const assoc = parseList(properties.associated_commodities);
    const idLabel = properties.jurisdiction_code === 'CA-BC'
        ? 'MINFILE'
        : properties.jurisdiction_code === 'CA-SK'
            ? 'SMDI'
            : 'ID';

    return (
        <>
            {properties.external_id && (
                <KV label={idLabel} value={`#${properties.external_id}`} mono />
            )}
            <KV label="Status" value={properties.status} mono />
            <KV label="Primary" value={primary.join(', ')} />
            {assoc.length > 0 && <KV label="Associated" value={assoc.join(', ')} />}
            {properties.production_flag && (
                <>
                    <span /> {/* grid spacer */}
                    <span style={{ color: '#e8a36b' }} className="text-[11px]">Has recorded production</span>
                </>
            )}
        </>
    );
}

function DrillholeFields({ properties }: { properties: Record<string, any> }) {
    const commodities = parseList(properties.commodity_of_interest);
    const hasDepth = properties.has_total_length === true
        || properties.has_total_length === 'true';
    return (
        <>
            {properties.drillhole_id && (
                <KV label="ID" value={properties.drillhole_id} mono />
            )}
            <KV label="Company" value={properties.company} />
            <KV label="Project" value={properties.project_name} />
            <KV label="Drill type" value={properties.drill_type} />
            {hasDepth && (
                <KV label="Depth" value={`${properties.total_length_m} m`} mono />
            )}
            {commodities.length > 0 && (
                <KV label="Target" value={commodities.join(', ')} />
            )}
            {properties.core_availability && properties.core_availability !== 'unknown' && (
                <KV label="Core" value={properties.core_availability} />
            )}
        </>
    );
}

function ResourcePotentialFields({ properties }: { properties: Record<string, any> }) {
    const hasRank = properties.has_potential_rank === true
        || properties.has_potential_rank === 'true';
    return (
        <>
            <KV label="Commodity" value={titleCase(properties.commodity)} mono />
            <KV
                label="Potential"
                value={hasRank ? `Rank ${properties.potential_rank} / 6` : '—'}
                mono
            />
            {properties.methodology_ref && (
                <>
                    <span />
                    <span className="text-[11px] leading-snug" style={{ color: 'var(--fg-3)' }}>
                        {properties.methodology_ref}
                    </span>
                </>
            )}
        </>
    );
}

function RockSampleFields({ properties }: { properties: Record<string, any> }) {
    return (
        <>
            {properties.sample_number && (
                <KV label="Sample" value={properties.sample_number} mono />
            )}
            {properties.station && (
                <KV label="Station" value={properties.station} mono />
            )}
            <KV label="Geologist" value={properties.geologist} />
            <KV label="Area" value={properties.geographic_area} />
            {properties.nts_250k && (
                <KV label="NTS 1:250K" value={properties.nts_250k} mono />
            )}
            {properties.report_number && (
                <KV label="Report" value={properties.report_number} mono />
            )}
        </>
    );
}

function AssessmentSurveyFields({ properties }: { properties: Record<string, any> }) {
    const typeLabel =
        properties.survey_type === 'airborne' ? 'Airborne survey'
        : properties.survey_type === 'ground' ? 'Ground survey'
        : properties.survey_type === 'underground' ? 'Underground survey'
        : 'Assessment survey';
    return (
        <>
            <KV label="Type" value={typeLabel} mono />
            <span />
            <span className="text-[11px] leading-snug" style={{ color: 'var(--fg-3)' }}>
                Survey footprint from the SMAD index. Detailed geophysics, geochemistry,
                and drilling reports are delivered via the linked assessment filing.
            </span>
        </>
    );
}

function GenericFields({ properties }: { properties: Record<string, any> }) {
    return (
        <>
            <KV
                label="Source"
                value={properties.source_id ?? properties.jurisdiction_code}
            />
            <KV label="Feature" value={properties.source_feature_id} mono />
        </>
    );
}

// ── Shared pieces ──────────────────────────────────────────────────────

function KV({
    label,
    value,
    mono = false,
}: {
    label: string;
    value: string | number | null | undefined;
    mono?: boolean;
}) {
    if (value == null || value === '') return null;
    return (
        <>
            <span style={{ color: 'var(--fg-3)' }}>{label}</span>
            <span
                className={mono ? 'font-mono text-right' : 'text-right'}
                style={{ color: 'var(--fg-1)' }}
            >
                {String(value)}
            </span>
        </>
    );
}

function SourceFooter({ properties }: { properties: Record<string, any> }) {
    const src = properties.source_url as string | undefined;
    if (!src) return null;
    return (
        <div className="mt-2 pt-2 border-t" style={{ borderColor: 'var(--line-1)' }}>
            <a
                href={src}
                target="_blank"
                rel="noopener noreferrer"
                className="text-[10px] font-mono uppercase tracking-wider"
                style={{ color: 'var(--accent)' }}
            >
                Open upstream record →
            </a>
        </div>
    );
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

function parseList(raw: unknown): string[] {
    if (!raw) return [];
    if (Array.isArray(raw)) return raw.map(String).filter(Boolean);
    if (typeof raw === 'string') {
        const s = raw.trim();
        if (!s) return [];
        if (s.startsWith('{') && s.endsWith('}')) {
            return s
                .slice(1, -1)
                .split(',')
                .map((p) => p.replace(/^"|"$/g, '').trim())
                .filter(Boolean);
        }
        try {
            const parsed = JSON.parse(s);
            if (Array.isArray(parsed)) return parsed.map(String);
        } catch {
            // fall through — treat as comma-separated
        }
        return s.split(',').map((p) => p.trim()).filter(Boolean);
    }
    return [];
}

function titleCase(value: unknown): string {
    if (!value) return '—';
    const s = String(value).trim();
    if (!s) return '—';
    return s[0].toUpperCase() + s.slice(1).toLowerCase();
}
