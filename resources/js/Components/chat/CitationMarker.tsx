// @ts-nocheck — migration in progress
import { BookOpen, Table2, Network, MapPin } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { Citation } from '@/types';

/**
 * Canonical citation kinds — Group 1 of the updated CITATION_RE.
 *
 * Dash-form:   [NI43-N], [PUB-N], [DATA-N], [PGEO-N]
 * Colon-form:  [NI43:N], [PUB:N], [DATA:N], [PGEO:N]  (Module 6 Chunk 3.6+)
 * Evidence-id: [ev:<uuid>]                             (future ev binding)
 */
export type MarkerKind = 'NI43' | 'PUB' | 'DATA' | 'PGEO' | 'ev';

/**
 * Evidence type values returned by GET /v1/evidence/{id}.
 * These override the kind-based icon when a full evidence fetch has happened.
 */
export type EvidenceType =
    | 'document_passage'
    | 'structured_record'
    | 'graph_edge'
    | 'map_feature';

interface Props {
    /** Kind prefix parsed from the citation marker — determines default icon */
    kind: MarkerKind;
    /** The id/index portion: "1" for [DATA:1], a uuid string for [ev:<uuid>] */
    id: string;
    /**
     * Citation payload from the per-message SSE citation cache.
     * May be undefined if the marker appears in text before the citation event
     * arrives, or if it is an [ev:...] marker with no cache entry.
     */
    citation?: Citation | null;
    /** Called when the user clicks the marker. Receives the lookup data available. */
    onClick: (citation: Citation | null, kind: MarkerKind, id: string) => void;
}

// ── Icon map: kind → default Lucide icon ──────────────────────────────────
// Used when citation?.evidence_type is absent (SSE citation events don't yet
// carry evidence_type; that requires evidence_items rows from Module 3 B8.5).
const KIND_ICON: Record<MarkerKind, React.FC<React.SVGProps<SVGSVGElement>>> = {
    DATA:  Table2,    // structured tool result (PostGIS / Qdrant structured)
    NI43:  BookOpen,  // NI 43-101 document passage
    PUB:   BookOpen,  // public geoscience document passage
    PGEO:  MapPin,    // public geoscience map feature / structured
    ev:    BookOpen,  // evidence-id bound — evidence_type overrides when known
};

// ── Icon map: evidence_type → Lucide icon ─────────────────────────────────
// Applied only when citation carries a resolved evidence_type from the
// GET /v1/evidence/{id} payload. Overrides the kind-based default.
const EVIDENCE_TYPE_ICON: Record<EvidenceType, React.FC<React.SVGProps<SVGSVGElement>>> = {
    document_passage:  BookOpen,
    structured_record: Table2,
    graph_edge:        Network,
    map_feature:       MapPin,
};

// ── Aria-label map per icon type ──────────────────────────────────────────
const ARIA_LABELS: Record<EvidenceType | 'default', string> = {
    document_passage:  'Cite passage',
    structured_record: 'Cite table row',
    graph_edge:        'Cite graph edge',
    map_feature:       'Cite map feature',
    default:           'View source',
};

// ── Color palettes per kind ───────────────────────────────────────────────
const KIND_STYLE: Record<MarkerKind, { bg: string; border: string; text: string; hover: string }> = {
    NI43: {
        bg:     'bg-amber-900/60',
        border: 'border-amber-600/50',
        text:   'text-amber-300',
        hover:  'hover:bg-amber-800/70',
    },
    PUB: {
        bg:     'bg-blue-900/60',
        border: 'border-blue-600/50',
        text:   'text-blue-300',
        hover:  'hover:bg-blue-800/70',
    },
    DATA: {
        bg:     'bg-green-900/60',
        border: 'border-green-600/50',
        text:   'text-green-300',
        hover:  'hover:bg-green-800/70',
    },
    PGEO: {
        bg:     'bg-rose-900/60',
        border: 'border-rose-600/50',
        text:   'text-rose-300',
        hover:  'hover:bg-rose-800/70',
    },
    ev: {
        bg:     'bg-violet-900/60',
        border: 'border-violet-600/50',
        text:   'text-violet-300',
        hover:  'hover:bg-violet-800/70',
    },
};

export function CitationMarker({ kind, id, citation, onClick }: Props) {
    // Determine which icon to show: evidence_type overrides kind default.
    const evidenceType = citation?.evidence_type as EvidenceType | undefined;
    const Icon = evidenceType && EVIDENCE_TYPE_ICON[evidenceType]
        ? EVIDENCE_TYPE_ICON[evidenceType]
        : KIND_ICON[kind] ?? BookOpen;

    const ariaLabel = evidenceType && ARIA_LABELS[evidenceType]
        ? ARIA_LABELS[evidenceType]
        : ARIA_LABELS['default'];

    const style = KIND_STYLE[kind] ?? KIND_STYLE['DATA'];

    // Reconstruct the visible marker text. Use colon-form as canonical display.
    const displayText = kind === 'ev' ? `ev:${id}` : `${kind}:${id}`;
    // Keep it brief in the chip — just the superscript index for numeric ids.
    const indexLabel = /^\d+$/.test(id) ? id : id.slice(0, 6);

    return (
        <button
            type="button"
            onClick={() => onClick(citation ?? null, kind, id)}
            className={cn(
                'inline-flex items-center gap-0.5',
                style.bg,
                style.border,
                style.text,
                style.hover,
                'border rounded',
                'text-xs font-mono font-medium',
                'px-1.5 py-0.5',
                'mx-0.5',
                'cursor-pointer',
                'transition-colors duration-150',
                'focus:outline-none focus:ring-2 focus:ring-offset-1 focus:ring-offset-gray-900 focus:ring-current',
            )}
            aria-label={`${ariaLabel} [${displayText}]${citation?.document_title ? ` from ${citation.document_title}` : ''}`}
            title={citation?.document_title ? `[${displayText}] — ${citation.document_title}` : `[${displayText}]`}
        >
            <Icon className="w-3 h-3 shrink-0" aria-hidden="true" />
            <sup className="text-[9px] leading-none ml-0.5">{indexLabel}</sup>
        </button>
    );
}
