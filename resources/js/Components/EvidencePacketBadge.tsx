/**
 * Plan §3a/§3b — typed-evidence summary strip.
 *
 * Rendered below assistant messages when the backend's
 * agentic_retrieval graph produced a typed EvidencePacket (see
 * `src/fastapi/app/agent/evidence_converter.py` +
 * `app/agent/authority.py`). Shows:
 *
 *   - A small chip per evidence kind with the count, in authority
 *     order (DocumentEvidence first, etc.).
 *
 * The context-window "Budget N" pill was removed (2026-10-04): a token
 * count is engineering telemetry, not something a geologist can act on.
 * So was the "Graph paths" label — there is no graph store (Neo4j was
 * removed 2026-07-28, CLAUDE.md hard rule 9).
 *
 * Reads `evidencePacket.evidence[].kind` — the dict comes from `GeoRAGResponse.evidence_packet` (the
 * `.model_dump()` form of the typed Pydantic `EvidencePacket`).
 *
 * Renders nothing when `packet` is null/undefined OR when the evidence
 * list is empty (the agentic graph wasn't engaged, or the converter
 * produced no extractable rows).
 */

import { useMemo } from 'react';

interface EvidenceEntry {
    kind: string;
    [k: string]: unknown;
}

interface EvidencePacketShape {
    evidence?: EvidenceEntry[];
    remaining_budget?: number;
    total_tokens?: number;
    system_prompt_tokens?: number;
    tool_plan?: string;
}

interface Props {
    packet: Record<string, unknown> | null | undefined;
}

const KIND_LABELS: Record<string, string> = {
    document: 'Documents',
    table: 'Tables',
    assay: 'Assays',
    collar: 'Collars',
    spatial: 'Spatial',
};

// Tight, monospace style consistent with the rest of Chat.tsx's footer
// metadata (timestamps, confidence pills) — purposefully low-contrast so
// the strip reads as ambient telemetry, not primary content.
const baseChipClass =
    'inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-[10px] font-mono uppercase tracking-wider';

export default function EvidencePacketBadge({ packet }: Props) {
    // Memo the count map so the chip strip stays referentially stable
    // when the parent re-renders without a packet change.
    const summary = useMemo(() => {
        if (!packet) return null;
        const typed = packet as EvidencePacketShape;
        const evidence = Array.isArray(typed.evidence) ? typed.evidence : [];
        if (evidence.length === 0) return null;
        const counts: Record<string, number> = {};
        for (const e of evidence) {
            const kind = String(e?.kind ?? 'unknown');
            counts[kind] = (counts[kind] ?? 0) + 1;
        }
        // Preserve a stable order: known kinds first (authority-leaning),
        // unknown kinds after, alphabetised within each bucket.
        const knownOrder = ['document', 'table', 'assay', 'collar', 'spatial'];
        const known = knownOrder.filter((k) => counts[k]);
        const unknown = Object.keys(counts)
            .filter((k) => !knownOrder.includes(k))
            .sort();
        const ordered = [...known, ...unknown];
        return { counts, ordered };
    }, [packet]);

    if (!summary) return null;

    return (
        <div className="mt-2 flex flex-wrap items-center gap-1.5" data-testid="evidence-packet-badge">
            {summary.ordered.map((kind) => {
                const label = KIND_LABELS[kind] ?? kind;
                return (
                    <span
                        key={kind}
                        className={baseChipClass}
                        style={{
                            background: 'var(--bg-3, rgba(120, 120, 120, 0.1))',
                            color: 'var(--fg-3, #888)',
                        }}
                        title={`${summary.counts[kind]} ${label.toLowerCase()} in evidence packet`}
                    >
                        <span>{label}</span>
                        <span className="opacity-70">×{summary.counts[kind]}</span>
                    </span>
                );
            })}
        </div>
    );
}
