// @ts-nocheck — migration in progress
/**
 * ConflictCards — B8
 *
 * Renders each conflicting-evidence entry as a shadcn Card with two (or more)
 * sub-panels side-by-side — one per value — showing the supporting evidence_id
 * as a clickable CitationMarker-style chip that opens EvidenceInspector.
 *
 * Global Invariant 7 (spec): NEVER auto-pick a winner — always surface both
 * values and let the geologist decide.
 *
 * Shape (from src/fastapi/app/models/rag.py lines 284–310):
 *   conflicting_evidence: Array<{
 *     entity_key:    string     — the entity being described (e.g. "DH-001")
 *     property_name: string     — the property with conflicting values (e.g. "total_depth")
 *     evidence_ids:  string[]   — parallel array with value[i]
 *     values:        string[]   — parallel array with evidence_id[i]
 *   }>
 *
 * A11y:
 *   - Outer region: role="region" + aria-labelledby pointing to warning heading
 *   - Each conflict: <figure> with <figcaption>
 *   - Each evidence chip: aria-label with evidence context
 */

import { cn } from '@/lib/utils';

// ── Types ─────────────────────────────────────────────────────────────────

export interface ConflictEntry {
    entity_key: string;
    property_name: string;
    evidence_ids: string[];
    values: string[];
}

interface ConflictCardsProps {
    conflicts: ConflictEntry[] | null | undefined;
    /** Called when a chip is clicked — receives the evidence_id string */
    onInspectEvidence?: (evidenceId: string) => void;
}

// ── Evidence chip (inline, no external dependency on CitationMarker) ──────

interface EvidenceChipProps {
    evidenceId: string;
    label: string;
    onInspect?: (evidenceId: string) => void;
}

function EvidenceChip({ evidenceId, label, onInspect }: EvidenceChipProps) {
    return (
        <button
            type="button"
            onClick={() => onInspect?.(evidenceId)}
            className={cn(
                'inline-flex items-center gap-1 px-2 py-0.5 rounded-full',
                'text-[11px] font-mono text-amber-300',
                'bg-amber-950/40 border border-amber-700/50',
                'hover:bg-amber-900/50 hover:border-amber-500/60',
                'focus:outline-none focus:ring-2 focus:ring-amber-500',
                'transition-colors cursor-pointer',
            )}
            aria-label={`${label} — view evidence ${evidenceId.slice(0, 8)}…`}
            data-testid={`evidence-chip-${evidenceId}`}
        >
            {/* Small dot indicator */}
            <span className="w-1.5 h-1.5 rounded-full bg-amber-500 shrink-0" aria-hidden="true" />
            {evidenceId.length > 12 ? `ev:${evidenceId.slice(0, 8)}…` : `ev:${evidenceId}`}
        </button>
    );
}

// ── Single conflict card ──────────────────────────────────────────────────

interface ConflictCardProps {
    conflict: ConflictEntry;
    index: number;
    onInspectEvidence?: (evidenceId: string) => void;
}

function ConflictCard({ conflict, index, onInspectEvidence }: ConflictCardProps) {
    const { entity_key, property_name, evidence_ids, values } = conflict;
    const figcaptionId = `conflict-caption-${index}`;

    // Build parallel pairs; guard against mismatched arrays
    const pairs: Array<{ value: string; evidenceId: string }> = [];
    const len = Math.min(values.length, evidence_ids.length);
    for (let i = 0; i < len; i++) {
        pairs.push({ value: values[i], evidenceId: evidence_ids[i] });
    }

    return (
        <figure
            className="border border-amber-800/60 rounded-lg bg-amber-950/20 overflow-hidden"
            aria-labelledby={figcaptionId}
        >
            <figcaption
                id={figcaptionId}
                className="px-3 py-2 bg-amber-950/30 border-b border-amber-800/40 text-xs font-medium text-amber-200"
            >
                <span className="font-mono text-amber-400">{entity_key}</span>
                <span className="text-amber-300/60 mx-1.5">·</span>
                <span className="text-amber-200">{property_name}</span>
            </figcaption>

            {/* Side-by-side value panels */}
            <div className={cn(
                'grid gap-px bg-amber-900/20',
                pairs.length === 2 ? 'grid-cols-2' : 'grid-cols-1',
            )}>
                {pairs.map(({ value, evidenceId }, i) => (
                    <div
                        key={evidenceId || i}
                        className="bg-gray-900 px-3 py-2.5 flex flex-col gap-1.5"
                    >
                        <span className="text-xs text-gray-400 uppercase tracking-wide">
                            Value {i + 1}
                        </span>
                        <p className="text-sm text-gray-100 break-words leading-snug">{value}</p>
                        <EvidenceChip
                            evidenceId={evidenceId}
                            label={`Value ${i + 1} for ${property_name} on ${entity_key}`}
                            onInspect={onInspectEvidence}
                        />
                    </div>
                ))}
            </div>
        </figure>
    );
}

// ── Main export ────────────────────────────────────────────────────────────

const REGION_LABEL_ID = 'conflict-cards-heading';

export function ConflictCards({ conflicts, onInspectEvidence }: ConflictCardsProps) {
    if (!conflicts || conflicts.length === 0) return null;

    return (
        <section
            role="region"
            aria-labelledby={REGION_LABEL_ID}
            className="mt-2 w-full"
            data-testid="conflict-cards"
        >
            <h3
                id={REGION_LABEL_ID}
                className="text-xs font-semibold text-amber-400 mb-2 flex items-center gap-1.5"
            >
                {/* Warning triangle (inline SVG — no additional dep) */}
                <svg
                    xmlns="http://www.w3.org/2000/svg"
                    viewBox="0 0 24 24"
                    fill="currentColor"
                    className="w-3.5 h-3.5 shrink-0"
                    aria-hidden="true"
                >
                    <path fillRule="evenodd" d="M9.401 3.003c1.155-2 4.043-2 5.197 0l7.355 12.748c1.154 2-.29 4.5-2.599 4.5H4.645c-2.309 0-3.752-2.5-2.598-4.5L9.4 3.003ZM12 8.25a.75.75 0 0 1 .75.75v3.75a.75.75 0 0 1-1.5 0V9a.75.75 0 0 1 .75-.75Zm0 8.25a.75.75 0 1 0 0-1.5.75.75 0 0 0 0 1.5Z" clipRule="evenodd" />
                </svg>
                Conflicting evidence detected — review before relying on this answer
            </h3>

            <div className="space-y-2">
                {conflicts.map((conflict, i) => (
                    <ConflictCard
                        key={`${conflict.entity_key}-${conflict.property_name}-${i}`}
                        conflict={conflict}
                        index={i}
                        onInspectEvidence={onInspectEvidence}
                    />
                ))}
            </div>
        </section>
    );
}
