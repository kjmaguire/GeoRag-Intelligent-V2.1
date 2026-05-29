import { useEffect, useState } from 'react';
import type { Citation, SourceData, PgeoSourceChunkIdParts } from '@/types';
import { formatStaleness } from '@/lib/time';
import { Badge } from '@/Components/ui/badge';
import EntityReferencesDrillIn from './EntityReferencesDrillIn';

/**
 * Jurisdiction-aware citation detail card for Public Geoscience citations
 * (plan §08 "Stage 2 — post-generation span resolution").
 *
 * Renders inside the existing chat citation panel when citation_type === 'PGEO'.
 *
 * Two-stage data loading:
 *   1. Primary fields come from the Citation itself (already delivered by the
 *      SSE completed event) — jurisdiction name, license, staleness, upstream
 *      URL. This is instant, no fetch required.
 *   2. Full entity body + references summary are fetched lazily from
 *      /api/v1/citations/resolve when the user opens the detail card, so
 *      the panel stays snappy for users who just want the header info.
 *
 * The layout mirrors the sample in plan §08:
 *     Mineral Deposits Index -- Saskatchewan Geological Survey
 *     SMDI 0123 -- Star Lake Showing -- Gold, primary
 *     Source: gis.saskatchewan.ca -- last refreshed 2026-04-09
 *     [Open in GeoHub link]
 *     [License tag]
 *     [Referenced in N reports] drill-in
 */

interface CitationPGEODetailProps {
    citation: Citation;
}

export default function CitationPGEODetail({ citation }: CitationPGEODetailProps) {
    const [resolved, setResolved] = useState<SourceData | null>(null);
    const [resolving, setResolving] = useState<boolean>(false);
    const [resolveError, setResolveError] = useState<string | null>(null);
    const [showDetails, setShowDetails] = useState<boolean>(false);

    // Parse the source_chunk_id so the drill-in component can call the
    // entity-scoped references endpoint without re-parsing.
    const parts = parseSourceChunkId(citation.source_chunk_id);

    // Lazy-resolve body when the user expands the detail block.
    useEffect(() => {
        if (!showDetails || resolved || resolving) return;
        void loadResolverBody(citation, { setResolved, setResolving, setResolveError });
    }, [showDetails, resolved, resolving, citation]);

    const staleness = formatStaleness(citation.staleness_seconds ?? null);
    const sourceHost = citation.source_url ? safeHost(citation.source_url) : null;

    // Authority header — source name em-dash authority. Source name comes
    // from the resolver envelope; fall back to the Citation.document_title
    // which is already jurisdiction-qualified ("Saskatchewan -- ...").
    const authorityLine = resolved?.source?.name && resolved?.jurisdiction?.authority
        ? `${resolved.source.name} -- ${resolved.jurisdiction.authority}`
        : citation.document_title;

    return (
        <div className="space-y-3">
            <header className="space-y-1">
                <div className="text-[11px] uppercase tracking-wider text-rose-400 font-semibold">
                    {citation.jurisdiction_name ?? citation.jurisdiction_code ?? 'Public Geoscience'}
                </div>
                <h3 className="text-sm font-semibold text-gray-100 leading-snug">
                    {authorityLine}
                </h3>
                <p className="text-[11px] text-gray-500 leading-snug">
                    {sourceHost ?? resolved?.source?.service_url ?? 'public geoscience record'}
                    {citation.staleness_seconds != null && (
                        <>
                            {' - '}
                            <span
                                className={
                                    staleness.level === 'fresh'
                                        ? 'text-gray-500'
                                        : staleness.level === 'stale'
                                            ? 'text-amber-400'
                                            : 'text-red-400'
                                }
                                title={staleness.long_label}
                            >
                                last refreshed {staleness.label}
                            </span>
                        </>
                    )}
                </p>
            </header>

            {/* License tag -- small, links to full terms. Plan section 08 */}
            {/* mandatory attribution. */}
            {citation.license_summary && (
                <div className="flex items-start gap-2">
                    <Badge
                        variant="outline"
                        className="bg-gray-950/50 border-gray-700 text-gray-400 text-[10px] leading-tight max-w-full"
                    >
                        <span className="mr-1 text-gray-500" aria-hidden="true">(c)</span>
                        <span className="truncate">{citation.license_summary}</span>
                    </Badge>
                    {citation.license_url && (
                        <a
                            href={citation.license_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-[10px] text-gray-500 hover:text-amber-300 underline whitespace-nowrap"
                        >
                            terms
                        </a>
                    )}
                </div>
            )}

            {/* Upstream deep link. */}
            {citation.source_url && (
                <a
                    href={citation.source_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 text-xs text-rose-300 hover:text-rose-200 bg-rose-950/40 hover:bg-rose-950/60 border border-rose-800/50 hover:border-rose-700 rounded px-2 py-1 transition-colors"
                >
                    Open upstream record
                </a>
            )}

            {/* Staleness warning -- only shown when data is older than the */}
            {/* fresh threshold. Keeps the UI quiet for fresh data. */}
            {staleness.level !== 'fresh' && citation.staleness_seconds != null && (
                <p className="text-[11px] text-amber-300/80 leading-snug bg-amber-950/30 border border-amber-800/40 rounded px-2 py-1.5">
                    <span className="font-semibold">Cached data:</span> {staleness.long_label}.
                    The upstream may have newer records.
                </p>
            )}

            {/* View details -- lazy-fetch full entity body + references summary */}
            <div>
                <button
                    type="button"
                    onClick={() => setShowDetails(v => !v)}
                    className="text-xs text-rose-400 hover:text-rose-300 border border-rose-800/50 hover:border-rose-700 bg-rose-950/30 hover:bg-rose-950/50 rounded px-2 py-1 transition-colors w-full text-left"
                >
                    {resolving ? 'Loading details...' : showDetails ? 'Hide details' : 'View details'}
                </button>

                {resolveError && (
                    <p className="text-[11px] text-red-400 mt-1">Failed to load: {resolveError}</p>
                )}

                {showDetails && resolved && (
                    <div className="mt-2 space-y-3">
                        {/* Narrative summary (from FastAPI NL summary builder) */}
                        {resolved.text && (
                            <p className="text-xs text-gray-300 leading-relaxed whitespace-pre-wrap bg-gray-800 border border-gray-700 rounded-lg p-3">
                                {resolved.text}
                            </p>
                        )}

                        {/* Structured entity fields */}
                        {resolved.entity && (
                            <EntityFieldsList entity={resolved.entity} canonicalType={parts?.canonical_type} />
                        )}

                        {/* Cross-corpus references drill-in */}
                        {parts && (
                            <EntityReferencesDrillIn
                                canonicalType={parts.canonical_type}
                                pgId={parts.pg_id}
                                summary={resolved.references_summary ?? null}
                            />
                        )}
                    </div>
                )}
            </div>
        </div>
    );
}

// ── Helpers ──────────────────────────────────────────────────────────

async function loadResolverBody(
    citation: Citation,
    handlers: {
        setResolved: (d: SourceData) => void;
        setResolving: (v: boolean) => void;
        setResolveError: (e: string | null) => void;
    },
): Promise<void> {
    handlers.setResolving(true);
    handlers.setResolveError(null);
    try {
        // Auth via Sanctum session cookie (same-origin). No bearer token from
        // localStorage — localStorage is an XSS-exfiltration target (types.ts:11-12).
        const res = await fetch(
            `/api/v1/citations/resolve?source_chunk_id=${encodeURIComponent(citation.source_chunk_id)}&citation_type=${citation.citation_type}`,
            {
                credentials: 'same-origin',
                headers: {
                    Accept: 'application/json',
                },
            },
        );
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data: SourceData = await res.json();
        handlers.setResolved(data);
    } catch (err) {
        handlers.setResolveError(err instanceof Error ? err.message : String(err));
    } finally {
        handlers.setResolving(false);
    }
}

/**
 * Parse pg_canonical_type:source_id:feature=fid:pg_id=uuid
 * Returns null for any other shape (including legacy NI43/PUB/DATA chunk IDs).
 */
export function parseSourceChunkId(raw: string): PgeoSourceChunkIdParts | null {
    const m = /^pg_([a-z0-9_]+):([^:]+)(?::feature=([^:]*))?(?::pg_id=([^:]+))?$/.exec(raw);
    if (!m) return null;
    const canonicalType = m[1];
    if (
        canonicalType !== 'mine' &&
        canonicalType !== 'mineral_occurrence' &&
        canonicalType !== 'drillhole_collar' &&
        canonicalType !== 'resource_potential_zone'
    ) {
        return null;
    }
    return {
        canonical_type: canonicalType,
        source_id: m[2],
        feature_id: m[3] || null,
        pg_id: m[4] || null,
    };
}

function safeHost(url: string): string | null {
    try {
        return new URL(url).host;
    } catch {
        return null;
    }
}

// ── Structured entity fields ────────────────────────────────────────

interface EntityFieldsListProps {
    entity: Record<string, unknown>;
    canonicalType?: PgeoSourceChunkIdParts['canonical_type'] | null;
}

function EntityFieldsList({ entity, canonicalType }: EntityFieldsListProps) {
    // Pick the most meaningful fields per canonical_type so geologists see
    // what they care about up top. The full entity blob is still available
    // below for auditing.
    const highlighted = pickHighlightedFields(entity, canonicalType);

    return (
        <dl className="text-xs bg-gray-800/60 border border-gray-700 rounded-lg p-3 space-y-1.5">
            {highlighted.map(([label, value]) => (
                <div key={label} className="flex items-start justify-between gap-3">
                    <dt className="text-gray-500 shrink-0">{label}</dt>
                    <dd className="text-gray-200 text-right break-words min-w-0">{formatValue(value)}</dd>
                </div>
            ))}
        </dl>
    );
}

function pickHighlightedFields(
    entity: Record<string, unknown>,
    canonicalType?: PgeoSourceChunkIdParts['canonical_type'] | null,
): Array<[string, unknown]> {
    // Field picker per canonical type; we intentionally pull from nested
    // paths too (entity.commodities array etc.).
    const pick = (keys: Array<[string, string]>) =>
        keys
            .map<[string, unknown] | null>(([label, key]) => {
                const v = entity[key];
                return v === undefined || v === null || v === '' ? null : [label, v];
            })
            .filter((v): v is [string, unknown] => v !== null);

    switch (canonicalType) {
        case 'mine':
            return pick([
                ['Name', 'name'],
                ['Status', 'status'],
                ['Commodities', 'commodities'],
                ['Operator', 'operator'],
            ]);
        case 'mineral_occurrence':
            return pick([
                // Column was renamed `smdi_id` -> `external_id` in V1.2;
                // label stays jurisdiction-agnostic since CA-BC etc. use
                // their own native ID schemes (MINFILE, MODS, ...).
                ['External ID', 'external_id'],
                ['Name', 'name'],
                ['Status', 'status'],
                ['Primary commodities', 'primary_commodities'],
                ['Associated', 'associated_commodities'],
                ['Grouping', 'commodity_grouping'],
                ['Discovery type', 'discovery_type'],
                ['Production', 'production_flag'],
            ]);
        case 'drillhole_collar':
            return pick([
                ['Hole ID', 'drillhole_id'],
                ['Hole name', 'drillhole_name'],
                ['Company', 'company'],
                ['Project', 'project_name'],
                ['Drilled', 'date_drilled'],
                ['Drill type', 'drill_type'],
                ['Target', 'commodity_of_interest'],
                ['Total depth (m)', 'total_length_m'],
                ['Core', 'core_availability'],
            ]);
        case 'resource_potential_zone':
            return pick([
                ['Commodity', 'commodity'],
                ['Rank', 'potential_rank'],
                ['Methodology', 'methodology_ref'],
            ]);
        default:
            // Generic dump of the first handful of scalar fields.
            return Object.entries(entity)
                .filter(([, v]) => typeof v !== 'object' || v === null)
                .slice(0, 8) as Array<[string, unknown]>;
    }
}

function formatValue(value: unknown): string {
    if (value === null || value === undefined) return '-';
    if (Array.isArray(value)) {
        const clean = value
            .map(v => (v == null ? '' : String(v)))
            .filter(Boolean);
        return clean.length > 0 ? clean.join(', ') : '-';
    }
    if (typeof value === 'boolean') return value ? 'yes' : 'no';
    return String(value);
}
