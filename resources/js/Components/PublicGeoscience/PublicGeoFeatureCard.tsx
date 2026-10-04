import { useEffect, useState } from 'react';
import { PUBLIC_GEO_LAYER_LABELS, type PublicGeoFeature } from '@/Components/MapView';
import {
    POLYGON_FIELDS,
    POLYGON_LAYER_LABELS,
    type PolygonFeatureProperties,
    type SourceAttribution,
} from '@/Components/PublicGeoscience/polygonLayers';

/**
 * PublicGeoFeatureCard — the clicked-record panel on the Public Geoscience map.
 *
 * Deliberately the same surface as the Workspace map's clicked-hole card
 * (WorkspaceMap.tsx, "Clicked hole detail"): anchored top-left over the map,
 * `--bg-1` / `--line-2`, an uppercase eyebrow with a ✕, the record name, then
 * a two-column label / value grid in monospace. A geologist moving between
 * their own project's collars and the province's public drillholes reads
 * both the same way.
 *
 * Point records (mines, occurrences, public drillholes, rock samples) carry
 * only a name on the map feed, so the full row is fetched on click from the
 * PGEO citation resolver — the same endpoint, and therefore the same fields,
 * a chat citation of that record opens. Polygons already carry their key
 * attributes on the map feed and render without a fetch.
 */

export type PointLayer = PublicGeoFeature['properties']['layer'];

export type PublicGeoSelection =
    | {
          kind: 'point';
          layer: PointLayer;
          id: string;
          sourceId: string;
          label: string | null;
          jurisdiction: string;
          lngLat: [number, number];
      }
    | { kind: 'polygon'; props: PolygonFeatureProperties };

/** What /api/v1/citations/resolve returns for a `pg_*` source_chunk_id. */
export interface PgeoResolved {
    title: string | null;
    jurisdiction: { code: string | null; name: string | null; authority: string | null };
    source: { source_id: string | null; name: string | null; service_url: string | null };
    license: { summary: string | null; url: string | null };
    refresh: { last_refreshed_at: string | null };
    references_summary: {
        count: number;
        documents: Array<{ document_id: string; title: string | null; filename: string | null }>;
    };
    entity: Record<string, unknown> | null;
}

/** The resolver key for one point record. See AbstractPgeoResolver::parseChunkId. */
export function pgeoChunkId(layer: PointLayer, sourceId: string, id: string): string {
    return `pg_${layer}:${sourceId}:pg_id=${id}`;
}

const EYEBROW: Record<PointLayer, string> = {
    drillhole_collar: 'Public drillhole',
    mine: 'Mine',
    mineral_occurrence: 'Mineral occurrence',
    rock_sample: 'Rock sample',
};

/**
 * Stratigraphic contacts CA-SK-DRILLHOLE publishes, in the keys sync.py
 * lifts them into (`_STRAT_CONTACTS`). Shown exactly as reported — a depth
 * per named contact — with no interval between two contacts labelled as a
 * unit: which rock sits between them is the survey's call, not ours.
 */
export const STRAT_CONTACT_LABELS: Record<string, string> = {
    base_of_quaternary: 'Base of Quaternary',
    base_of_phanerozoic: 'Base of Phanerozoic',
    base_of_athabasca_sg: 'Base of Athabasca SG',
    top_crystalline_basement: 'Top of crystalline basement',
};

const STRAT_CONTACT_COLORS: Record<string, string> = {
    base_of_quaternary: '#e8c46b',
    base_of_phanerozoic: '#7accee',
    base_of_athabasca_sg: '#e8a36b',
    top_crystalline_basement: '#c9a0f0',
};

export interface StratContact {
    key: string;
    label: string;
    depth_m: number;
    elevation_m: number | null;
}

/** Contacts with a usable depth, shallowest first. */
export function stratContacts(raw: unknown): StratContact[] {
    if (!raw || typeof raw !== 'object') return [];
    const out: StratContact[] = [];
    for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
        if (!value || typeof value !== 'object') continue;
        const depth = toNumber((value as Record<string, unknown>).depth_m);
        if (depth === null) continue;
        out.push({
            key,
            label: STRAT_CONTACT_LABELS[key] ?? key.replace(/_/g, ' '),
            depth_m: depth,
            elevation_m: toNumber((value as Record<string, unknown>).elevation_m),
        });
    }
    return out.sort((a, b) => a.depth_m - b.depth_m);
}

function toNumber(v: unknown): number | null {
    if (v === null || v === undefined || v === '') return null;
    const n = typeof v === 'number' ? v : Number(v);
    return Number.isFinite(n) ? n : null;
}

function text(v: unknown): string | null {
    if (v === null || v === undefined) return null;
    if (Array.isArray(v)) return v.length ? v.join(', ') : null;
    const s = String(v).trim();
    return s === '' ? null : s;
}

function metres(v: unknown): string | null {
    const n = toNumber(v);
    return n === null ? null : `${n.toFixed(1)} m`;
}

function degrees(v: unknown): string | null {
    const n = toNumber(v);
    return n === null ? null : `${n.toFixed(0)}°`;
}

/** Only an http(s) URL may become a link — upstream data, not ours. */
function safeHref(url: string | null | undefined): string | null {
    if (!url) return null;
    return /^https?:\/\//i.test(url) ? url : null;
}

type Row = [label: string, value: string | null];

/** Label / value rows for a resolved point record, per layer. Empty values drop out. */
export function pointRows(layer: PointLayer, e: Record<string, unknown>): Row[] {
    switch (layer) {
        case 'drillhole_collar': {
            const dip = degrees(e.inclination_deg);
            const az = degrees(e.azimuth_deg);
            return [
                ['Hole ID', text(e.drillhole_id)],
                ['Total depth', metres(e.total_length_m)],
                ['Dip / azimuth', dip || az ? `${dip ?? '—'} / ${az ?? '—'}` : null],
                ['Collar elevation', metres(e.collar_elevation_m)],
                ['Company', text(e.company)],
                ['Project', text(e.project_name)],
                ['Drilled', text(e.date_drilled)],
                ['Drill type', text(e.drill_type)],
                ['Commodity', text(e.commodity_of_interest)],
                ['Core', e.core_availability && e.core_availability !== 'unknown' ? text(e.core_availability) : null],
                ['Core storage', text(e.core_storage)],
                ['Disposition', text(e.disposition)],
            ];
        }
        case 'mine':
            return [
                ['Status', text(e.status)],
                ['Commodities', text(e.commodities)],
                ['Operator', text(e.operator)],
            ];
        case 'mineral_occurrence':
            return [
                ['SMDI #', text(e.external_id)],
                ['Status', text(e.status)],
                ['Primary', text(e.primary_commodities)],
                ['Associated', text(e.associated_commodities)],
                ['Discovery', text(e.discovery_type)],
                ['Production', e.production_flag === true ? 'yes' : null],
                ['Reserves / resources', text(e.reserves_resources)],
                ['Historic names', text(e.historic_names)],
            ];
        case 'rock_sample':
            return [
                ['Sample #', text(e.sample_number)],
                ['Station', text(e.station)],
                ['Geologist', text(e.geologist)],
                ['Collected', text(e.date_collected)],
                ['Area', text(e.geographic_area)],
                ['Report #', text(e.report_number)],
                ['Map #', text(e.map_number)],
                ['NTS 1:50k', text(e.nts_50k)],
            ];
    }
}

export default function PublicGeoFeatureCard({
    selection,
    sources,
    onClose,
}: {
    selection: PublicGeoSelection;
    /** Source attribution from the map response — polygons need it; points get it from the resolver. */
    sources?: Record<string, SourceAttribution>;
    onClose: () => void;
}) {
    const point = selection.kind === 'point' ? selection : null;
    const chunkId = point ? pgeoChunkId(point.layer, point.sourceId, point.id) : null;
    const [resolved, setResolved] = useState<{ chunkId: string; body: PgeoResolved } | null>(null);
    const [failed, setFailed] = useState<{ chunkId: string; message: string } | null>(null);

    useEffect(() => {
        if (!chunkId) return;
        const controller = new AbortController();
        fetch(`/api/v1/citations/resolve?source_chunk_id=${encodeURIComponent(chunkId)}&citation_type=PGEO`, {
            credentials: 'same-origin',
            headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            signal: controller.signal,
        })
            .then((res) => {
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                return res.json() as Promise<PgeoResolved>;
            })
            .then((body) => setResolved({ chunkId, body }))
            .catch((err) => {
                if (err instanceof DOMException && err.name === 'AbortError') return;
                setFailed({ chunkId, message: err instanceof Error ? err.message : String(err) });
            });
        return () => controller.abort();
    }, [chunkId]);

    // Derived rather than reset in the effect: a stale body for the previous
    // click is simply not the current one.
    const body = resolved && resolved.chunkId === chunkId ? resolved.body : null;
    const error = failed && failed.chunkId === chunkId ? failed.message : null;
    const loading = point !== null && body === null && error === null;

    let eyebrow: string;
    let title: string;
    let rows: Row[];
    let sourceName: string | null;
    let licence: { summary: string | null; url: string | null };
    let contacts: StratContact[] = [];
    let totalDepth: number | null = null;

    let jurisdiction: string;

    if (selection.kind === 'point') {
        const entity = body?.entity ?? null;
        jurisdiction = selection.jurisdiction;
        eyebrow = EYEBROW[selection.layer];
        title =
            text(entity?.drillhole_name) ??
            text(entity?.name) ??
            text(entity?.station) ??
            selection.label ??
            PUBLIC_GEO_LAYER_LABELS[selection.layer];
        rows = entity ? pointRows(selection.layer, entity) : [];
        sourceName = body?.source.name ?? selection.sourceId;
        licence = body?.license ?? { summary: null, url: null };
        if (entity && selection.layer === 'drillhole_collar') {
            contacts = stratContacts(entity.stratigraphic_depths);
            totalDepth = toNumber(entity.total_length_m);
        }
    } else {
        const props = selection.props;
        jurisdiction = props.jurisdiction_code;
        eyebrow = POLYGON_LAYER_LABELS[props.layer] ?? props.layer;
        title = props.label ?? eyebrow;
        rows = (POLYGON_FIELDS[props.layer] ?? []).map(([key, label]) => [label, text(props[key])] as Row);
        const src = sources?.[props.source_id];
        sourceName = src?.name ?? props.source_id;
        licence = { summary: src?.license_summary ?? null, url: src?.license_url ?? null };
    }

    const visibleRows = rows.filter(([, v]) => v !== null);
    const licenceHref = safeHref(licence.url);
    const references = body?.references_summary ?? null;

    return (
        <div
            role="dialog"
            aria-label={`${eyebrow}: ${title}`}
            className="absolute top-2 left-2 z-10 px-3 py-2 rounded border min-w-[240px] max-w-[280px] overflow-y-auto"
            style={{
                background: 'var(--bg-1)',
                borderColor: 'var(--line-2)',
                color: 'var(--fg-1)',
                maxHeight: 'calc(100% - 1rem)',
            }}
        >
            <div className="flex items-start justify-between gap-2">
                <div className="text-[11px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                    {eyebrow}
                </div>
                <button
                    type="button"
                    onClick={onClose}
                    aria-label="Close"
                    className="text-[10px] font-mono"
                    style={{ color: 'var(--fg-3)' }}
                >
                    ✕
                </button>
            </div>
            <div className="text-sm font-medium mt-0.5 break-words" style={{ color: 'var(--fg-0)' }}>
                {title}
            </div>
            <div className="text-[10px] font-mono mt-0.5" style={{ color: 'var(--fg-3)' }}>
                Public record · {jurisdiction}
                {point && ` · ${point.lngLat[1].toFixed(5)}, ${point.lngLat[0].toFixed(5)}`}
            </div>

            {loading && (
                <div className="mt-2 text-[11px] font-mono" style={{ color: 'var(--fg-3)' }}>
                    Loading record…
                </div>
            )}
            {error && (
                <div className="mt-2 text-[11px] font-mono text-red-400">Couldn't load this record ({error}).</div>
            )}
            {point && body && body.entity === null && (
                <div className="mt-2 text-[11px] font-mono" style={{ color: 'var(--fg-3)' }}>
                    This record is no longer in the synced data — it may have been removed upstream.
                </div>
            )}

            {visibleRows.length > 0 && (
                <div className="mt-2 grid grid-cols-2 gap-1 text-[11px]">
                    {visibleRows.map(([label, value]) => (
                        <div key={label} className="contents">
                            <div style={{ color: 'var(--fg-3)' }}>{label}</div>
                            <div className="font-mono text-right break-words" style={{ color: 'var(--fg-1)' }}>
                                {value}
                            </div>
                        </div>
                    ))}
                </div>
            )}

            {contacts.length > 0 && <StratContactsStrip contacts={contacts} totalDepth={totalDepth} />}

            {references && references.count > 0 && (
                <div
                    className="mt-2 text-[10px] font-mono px-2 py-1.5 rounded border"
                    style={{ color: 'var(--accent)', borderColor: 'var(--accent-dim)', background: 'var(--accent-bg)' }}
                >
                    Referenced in {references.count} ingested report{references.count === 1 ? '' : 's'}
                </div>
            )}

            <div
                className="mt-2 pt-2 border-t text-[10px] font-mono space-y-0.5"
                style={{ borderColor: 'var(--line-1)', color: 'var(--fg-3)' }}
            >
                <div>Source: {sourceName}</div>
                <div>
                    {licence.summary ? (
                        licenceHref ? (
                            <a
                                href={licenceHref}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="underline"
                                style={{ color: 'var(--fg-2)' }}
                            >
                                {licence.summary}
                            </a>
                        ) : (
                            licence.summary
                        )
                    ) : (
                        !loading && 'Licence unknown'
                    )}
                </div>
            </div>
        </div>
    );
}

/**
 * The published stratigraphic contacts drawn down the hole, to scale.
 *
 * A single column from collar to total depth (or the deepest contact, when
 * the survey lists no total depth), with a tick at each reported contact.
 * Intervals are left unfilled on purpose — see STRAT_CONTACT_LABELS.
 */
function StratContactsStrip({ contacts, totalDepth }: { contacts: StratContact[]; totalDepth: number | null }) {
    const deepest = contacts[contacts.length - 1].depth_m;
    const bottom = Math.max(totalDepth ?? 0, deepest) || 1;
    const H = 120;
    const y = (d: number) => 4 + (d / bottom) * (H - 8);

    return (
        <div className="mt-2">
            <div className="text-[10px] font-mono uppercase tracking-wider mb-1" style={{ color: 'var(--fg-3)' }}>
                Stratigraphic contacts
            </div>
            <div className="flex gap-2">
                <svg width="28" height={H} role="img" aria-label="Contacts down the hole" className="shrink-0">
                    <rect x="10" y="4" width="8" height={H - 8} rx="2" fill="var(--bg-2)" stroke="var(--line-2)" />
                    {contacts.map((c) => (
                        <line
                            key={c.key}
                            x1="4"
                            x2="24"
                            y1={y(c.depth_m)}
                            y2={y(c.depth_m)}
                            stroke={STRAT_CONTACT_COLORS[c.key] ?? '#9ca3af'}
                            strokeWidth="2"
                        />
                    ))}
                </svg>
                <div className="flex-1 space-y-1 text-[10px] font-mono">
                    {contacts.map((c) => (
                        <div key={c.key} className="flex items-start justify-between gap-2">
                            <span className="flex items-center gap-1.5" style={{ color: 'var(--fg-2)' }}>
                                <span
                                    className="w-2 h-2 rounded-full inline-block shrink-0"
                                    style={{ background: STRAT_CONTACT_COLORS[c.key] ?? '#9ca3af' }}
                                    aria-hidden="true"
                                />
                                {c.label}
                            </span>
                            <span className="text-right" style={{ color: 'var(--fg-1)' }}>
                                {c.depth_m.toFixed(1)} m
                            </span>
                        </div>
                    ))}
                    {totalDepth !== null && (
                        <div className="flex justify-between gap-2" style={{ color: 'var(--fg-3)' }}>
                            <span>Total depth</span>
                            <span>{totalDepth.toFixed(1)} m</span>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
