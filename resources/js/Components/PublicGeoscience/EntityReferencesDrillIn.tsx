import { useEffect, useState } from 'react';
import type { EntityReferencesResponse, SourceData } from '@/types';

/**
 * Drill-in affordance on a Public Geoscience citation card.
 *
 * Plan section 07d: "Canonical entity cards show a 'Referenced in N assessment
 * reports' section with drill-in to the linked SMAD documents, including the
 * signal(s) that established each link and the confidence."
 *
 * Rendering modes:
 *   - Collapsed preview: "Referenced in N reports" button. Shown as long as the
 *     envelope's references_summary indicates at least one active link.
 *   - Expanded list: lazy GET
 *     /api/v1/public-geoscience/entities/{canonical_type}/{pg_id}/references
 *     which returns the full set of documents for this entity, plus confidence
 *     and signal metadata.
 *
 * Confidence gating (plan section 07d):
 *   - High-confidence links (>= 0.9) render as a definitive relationship.
 *   - Medium (0.6-0.9) render with a "likely match" label and the signals visible.
 *   - Low (< 0.6) are stored but not surfaced by default; the drill-in toggles
 *     include-possible-matches via an "Include lower-confidence matches" toggle,
 *     which lowers the endpoint's min_confidence query param.
 *
 * Empty state: when the resolver envelope already says references_summary.count
 * is 0, we render a quiet "No assessment reports reference this record yet"
 * block instead of a loading spinner + empty list -- matches plan section 07d's
 * "empty state clean when no links exist".
 */

interface EntityReferencesDrillInProps {
    canonicalType: 'mine' | 'mineral_occurrence' | 'drillhole_collar' | 'resource_potential_zone';
    pgId: string | null;
    summary: SourceData['references_summary'];
}

export default function EntityReferencesDrillIn({
    canonicalType,
    pgId,
    summary,
}: EntityReferencesDrillInProps) {
    const [expanded, setExpanded] = useState<boolean>(false);
    const [includePossible, setIncludePossible] = useState<boolean>(false);
    const [data, setData] = useState<EntityReferencesResponse | null>(null);
    const [loading, setLoading] = useState<boolean>(false);
    const [error, setError] = useState<string | null>(null);

    const count = summary?.count ?? 0;

    // Reload when the user toggles confidence inclusion.
    useEffect(() => {
        if (!expanded || !pgId) return;
        void loadReferences(canonicalType, pgId, includePossible, {
            setData,
            setLoading,
            setError,
        });
    }, [expanded, pgId, canonicalType, includePossible]);

    // Empty state: no active links on this entity.
    if (count === 0) {
        return (
            <p className="text-[11px] text-gray-500 italic leading-snug">
                No assessment reports reference this record yet.
            </p>
        );
    }

    const label = `Referenced in ${count} assessment report${count === 1 ? '' : 's'}`;

    return (
        <div className="space-y-2">
            <button
                type="button"
                onClick={() => setExpanded(v => !v)}
                className="text-xs text-rose-400 hover:text-rose-300 border border-rose-800/50 hover:border-rose-700 bg-rose-950/30 hover:bg-rose-950/50 rounded px-2 py-1 transition-colors w-full text-left"
                aria-expanded={expanded}
            >
                {loading ? 'Loading references...' : expanded ? 'Hide references' : label}
            </button>

            {expanded && (
                <div className="space-y-2">
                    {/* Confidence gating toggle (plan section 07d). */}
                    <label className="flex items-center gap-2 text-[11px] text-gray-500">
                        <input
                            type="checkbox"
                            checked={includePossible}
                            onChange={e => setIncludePossible(e.target.checked)}
                            className="accent-rose-500"
                        />
                        Include lower-confidence matches (0.4 - 0.6)
                    </label>

                    {error && (
                        <p className="text-[11px] text-red-400">Failed to load: {error}</p>
                    )}

                    {data && data.documents.length === 0 && (
                        <p className="text-[11px] text-gray-500 italic">
                            No references found at confidence &ge; {data.min_confidence.toFixed(2)}.
                        </p>
                    )}

                    {data && data.documents.length > 0 && (
                        <ul className="space-y-1.5">
                            {data.documents.map((doc) => (
                                <ReferenceItem key={doc.document_id} doc={doc} />
                            ))}
                        </ul>
                    )}
                </div>
            )}
        </div>
    );
}

// ── Single reference item ────────────────────────────────────────────

interface ReferenceItemProps {
    doc: EntityReferencesResponse['documents'][number];
}

function ReferenceItem({ doc }: ReferenceItemProps) {
    const pct = Math.round(doc.confidence * 100);
    // Plan section 07d confidence gating — high/likely/possible.
    const level =
        doc.confidence >= 0.9 ? 'high' : doc.confidence >= 0.6 ? 'likely' : 'possible';

    const levelChip = {
        high:     { label: 'match',        cls: 'bg-emerald-950/40 text-emerald-300 border-emerald-800/50' },
        likely:   { label: 'likely match', cls: 'bg-amber-950/40 text-amber-300 border-amber-800/50' },
        possible: { label: 'possible',     cls: 'bg-gray-800 text-gray-400 border-gray-700' },
    }[level];

    return (
        <li className="bg-gray-800/60 border border-gray-700 rounded-lg p-2.5 space-y-1">
            <div className="flex items-start justify-between gap-2">
                <p className="text-xs text-gray-200 font-medium leading-snug break-words min-w-0">
                    {doc.title || doc.filename || doc.document_id}
                </p>
                <span
                    className={`text-[10px] font-mono px-1.5 py-0.5 rounded border ${levelChip.cls} shrink-0`}
                    title={`Confidence: ${pct}% (${level})`}
                >
                    {levelChip.label} · {pct}%
                </span>
            </div>
            <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-gray-500">
                {doc.company && <span>{doc.company}</span>}
                {doc.filing_date && <span>filed {doc.filing_date}</span>}
                {doc.commodity && <span>{doc.commodity}</span>}
            </div>
            {doc.signals.length > 0 && (
                <div className="flex flex-wrap gap-1">
                    {doc.signals.map(signal => (
                        <span
                            key={signal}
                            className="text-[10px] font-mono bg-gray-950/50 border border-gray-700 rounded px-1 py-0.5 text-gray-400"
                            title="Deterministic signal that established this link (plan section 07a)"
                        >
                            {signal}
                        </span>
                    ))}
                </div>
            )}
            {doc.extracted_context && (
                <p className="text-[10px] text-gray-500 italic leading-snug border-t border-gray-700 pt-1 mt-1">
                    &ldquo;{doc.extracted_context}&rdquo;
                </p>
            )}
        </li>
    );
}

// ── Fetch ───────────────────────────────────────────────────────────

async function loadReferences(
    canonicalType: EntityReferencesDrillInProps['canonicalType'],
    pgId: string,
    includePossible: boolean,
    handlers: {
        setData: (d: EntityReferencesResponse | null) => void;
        setLoading: (v: boolean) => void;
        setError: (e: string | null) => void;
    },
): Promise<void> {
    handlers.setLoading(true);
    handlers.setError(null);

    // Lower threshold when "include possible" is on (plan section 07d — low
    // confidence links are stored but not surfaced by default).
    const minConfidence = includePossible ? 0.4 : 0.6;

    try {
        // Auth via Sanctum session cookie (same-origin). No bearer token from
        // localStorage — localStorage is an XSS-exfiltration target (types.ts:11-12).
        const url =
            `/api/v1/public-geoscience/entities/${encodeURIComponent(canonicalType)}/${encodeURIComponent(pgId)}/references` +
            `?min_confidence=${minConfidence}`;
        const res = await fetch(url, {
            credentials: 'same-origin',
            headers: {
                Accept: 'application/json',
            },
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data: EntityReferencesResponse = await res.json();
        handlers.setData(data);
    } catch (err) {
        handlers.setError(err instanceof Error ? err.message : String(err));
    } finally {
        handlers.setLoading(false);
    }
}
