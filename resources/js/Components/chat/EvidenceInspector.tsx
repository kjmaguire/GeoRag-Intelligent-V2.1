import { useState, useEffect, useRef, useCallback } from 'react';
import { BookOpen, Table2, Network, MapPin, ExternalLink, RefreshCw, AlertCircle } from 'lucide-react';
import { CitationFeedbackButtons } from './CitationFeedbackButtons';
import {
    Sheet,
    SheetContent,
    SheetHeader,
    SheetTitle,
    SheetDescription,
} from '@/Components/ui/sheet';
import { ScrollArea } from '@/Components/ui/scroll-area';
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from '@/Components/ui/table';
import { cn } from '@/lib/utils';
import type { Citation } from '@/types';

// ── Type definitions ─────────────────────────────────────────────────────

type EvidenceType = 'document_passage' | 'structured_record' | 'graph_edge' | 'map_feature';

interface DocumentPassageEvidence {
    evidence_type: 'document_passage';
    passage_text: string;
    context_before?: string | null;
    context_after?: string | null;
    document_revision_id?: string | null;
    source_uri?: string | null;
    source_date?: string | null;
    page?: number | null;
    deep_link?: string | null;
    workspace_id?: string | null;
}

interface StructuredRecordEvidence {
    evidence_type: 'structured_record';
    structured_ref: Record<string, unknown>;
    lineage?: {
        bronze_uri?: string | null;
        parser_name?: string | null;
        parser_version?: string | null;
        ingestion_run_id?: string | null;
    } | null;
}

interface GraphEdgeEvidence {
    evidence_type: 'graph_edge';
    graph_edge_ref: Record<string, unknown>;
    start_node_labels?: string[];
    start_node_preview?: Record<string, unknown> | null;
    end_node_labels?: string[];
    end_node_preview?: Record<string, unknown> | null;
    described_in?: string[];
}

interface MapFeatureEvidence {
    evidence_type: 'map_feature';
    map_feature_ref?: Record<string, unknown> | null;
    tile_function?: string | null;
    bbox?: [number, number, number, number] | null;
    feature_properties?: Record<string, unknown> | null;
}

type EvidencePayload =
    | DocumentPassageEvidence
    | StructuredRecordEvidence
    | GraphEdgeEvidence
    | MapFeatureEvidence;

interface Props {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    /** UUID — triggers GET /v1/evidence/{evidenceId} when present */
    evidenceId?: string | null;
    /** Fallback when only the SSE citation payload is available */
    legacyCitation?: Citation | null;
    /** Phase H4 §12.8 — when both present, the inspector renders
     *  the 👍/👎 citation feedback footer. answerRunId comes from
     *  the message that owns the citation. */
    answerRunId?: string | null;
    workspaceId?: string | null;
}

// ── Auth helpers ──────────────────────────────────────────────────────────

// Auth rides the Sanctum session cookie (same-origin); credentials: 'same-origin'
// is set on each fetch call. No bearer token from localStorage —
// localStorage is an XSS-exfiltration target (types.ts:11-12).
function getAuthHeaders(): HeadersInit {
    const serviceKey = import.meta.env.VITE_SERVICE_KEY ?? '';
    const workspaceId =
        localStorage.getItem('georag_workspace_id') ??
        'a0000000-0000-0000-0000-000000000001';
    return {
        Accept: 'application/json',
        ...(serviceKey ? { 'x-service-key': serviceKey } : {}),
        'X-Workspace-Id': workspaceId,
    };
}

// ── Loading skeleton ──────────────────────────────────────────────────────

function InspectorSkeleton() {
    return (
        <div className="space-y-3 p-4 animate-pulse" aria-busy="true" aria-label="Loading evidence details">
            <div className="h-4 bg-gray-700 rounded w-3/4" />
            <div className="h-3 bg-gray-700 rounded w-1/2" />
            <div className="h-24 bg-gray-800 rounded" />
            <div className="h-3 bg-gray-700 rounded w-2/3" />
            <div className="h-3 bg-gray-700 rounded w-1/3" />
        </div>
    );
}

// ── Error states ──────────────────────────────────────────────────────────

function InspectorError({ status, onRetry }: { status: number | 'network'; onRetry: () => void }) {
    const message =
        status === 404
            ? 'Evidence not found (may have been pruned).'
            : status === 500
            ? 'Failed to load evidence details.'
            : 'Network error loading evidence.';

    return (
        <div className="flex flex-col items-center gap-3 p-6 text-center">
            <AlertCircle className="w-8 h-8 text-gray-500" aria-hidden="true" />
            <p className="text-sm text-gray-400">{message}</p>
            <button
                type="button"
                onClick={onRetry}
                className="inline-flex items-center gap-1.5 text-xs text-amber-400 hover:text-amber-300 border border-amber-800/60 hover:border-amber-600/60 bg-amber-950/30 hover:bg-amber-950/60 rounded px-3 py-1.5 transition-colors focus:outline-none focus:ring-2 focus:ring-amber-500"
            >
                <RefreshCw className="w-3 h-3" aria-hidden="true" />
                Retry
            </button>
        </div>
    );
}

// ── Branch renderers ──────────────────────────────────────────────────────

/** document_passage — highlighted passage + context + deep link */
function DocumentPassageRenderer({ evidence }: { evidence: DocumentPassageEvidence }) {
    return (
        <div className="space-y-3">
            {evidence.context_before && (
                <p className="text-xs text-gray-500 leading-relaxed italic border-l-2 border-gray-700 pl-2">
                    {evidence.context_before}
                </p>
            )}
            <div className="bg-amber-950/25 border border-amber-800/40 rounded-lg p-3">
                <p className="text-sm text-gray-100 leading-relaxed whitespace-pre-wrap">
                    {evidence.passage_text}
                </p>
            </div>
            {evidence.context_after && (
                <p className="text-xs text-gray-500 leading-relaxed italic border-l-2 border-gray-700 pl-2">
                    {evidence.context_after}
                </p>
            )}
            <div className="text-[11px] text-gray-600 space-y-0.5 border-t border-gray-800 pt-2">
                {evidence.source_uri && (
                    <p>
                        <span className="text-gray-500">Source:</span>{' '}
                        <span className="font-mono text-gray-400 break-all">{evidence.source_uri}</span>
                    </p>
                )}
                {evidence.source_date && (
                    <p>
                        <span className="text-gray-500">Date:</span>{' '}
                        <span className="text-gray-400">{evidence.source_date}</span>
                    </p>
                )}
                {evidence.page != null && (
                    <p>
                        <span className="text-gray-500">Page:</span>{' '}
                        <span className="text-gray-400">{evidence.page}</span>
                    </p>
                )}
            </div>
            {evidence.deep_link && (
                <a
                    href={evidence.deep_link}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 text-xs text-amber-400 hover:text-amber-300 border border-amber-800/60 hover:border-amber-600/60 bg-amber-950/30 rounded px-3 py-1.5 transition-colors focus:outline-none focus:ring-2 focus:ring-amber-500"
                >
                    <ExternalLink className="w-3 h-3" aria-hidden="true" />
                    Open document at page {evidence.page ?? '…'}
                </a>
            )}
        </div>
    );
}

/** structured_record — key-value table for structured_ref + lineage provenance */
function StructuredRecordRenderer({ evidence }: { evidence: StructuredRecordEvidence }) {
    const entries = Object.entries(evidence.structured_ref ?? {});
    const lineage = evidence.lineage;

    return (
        <div className="space-y-4">
            <div>
                <p className="text-[11px] uppercase tracking-wider text-gray-500 mb-1.5">Record fields</p>
                <Table>
                    <TableHeader>
                        <TableRow className="border-gray-700">
                            <TableHead className="text-gray-400 text-xs w-1/3">Field</TableHead>
                            <TableHead className="text-gray-400 text-xs">Value</TableHead>
                        </TableRow>
                    </TableHeader>
                    <TableBody>
                        {entries.length === 0 && (
                            <TableRow className="border-gray-700">
                                <TableCell colSpan={2} className="text-xs text-gray-500 text-center py-4">
                                    No fields
                                </TableCell>
                            </TableRow>
                        )}
                        {entries.map(([key, value]) => (
                            <TableRow key={key} className="border-gray-800">
                                <TableCell className="text-xs text-gray-400 font-mono">{key}</TableCell>
                                <TableCell className="text-xs text-gray-300 break-all">
                                    {value === null || value === undefined
                                        ? <span className="text-gray-600">—</span>
                                        : typeof value === 'object'
                                        ? <span className="font-mono text-gray-500">{JSON.stringify(value)}</span>
                                        : String(value)}
                                </TableCell>
                            </TableRow>
                        ))}
                    </TableBody>
                </Table>
            </div>

            {lineage && (
                <div>
                    <p className="text-[11px] uppercase tracking-wider text-gray-500 mb-1.5">Provenance</p>
                    <div className="bg-gray-800/60 border border-gray-700 rounded-lg p-3 text-[11px] space-y-1">
                        {lineage.bronze_uri && (
                            <p>
                                <span className="text-gray-500">Bronze URI:</span>{' '}
                                <span className="font-mono text-gray-400 break-all">{lineage.bronze_uri}</span>
                            </p>
                        )}
                        {lineage.parser_name && (
                            <p>
                                <span className="text-gray-500">Parser:</span>{' '}
                                <span className="text-gray-400">
                                    {lineage.parser_name}
                                    {lineage.parser_version && ` v${lineage.parser_version}`}
                                </span>
                            </p>
                        )}
                        {lineage.ingestion_run_id && (
                            <p>
                                <span className="text-gray-500">Run ID:</span>{' '}
                                <span className="font-mono text-gray-400">{lineage.ingestion_run_id}</span>
                            </p>
                        )}
                    </div>
                </div>
            )}
        </div>
    );
}

/** graph_edge — mini two-node display with described_in list */
function GraphEdgeRenderer({
    evidence,
    onDescribedInClick,
}: {
    evidence: GraphEdgeEvidence;
    onDescribedInClick?: (id: string) => void;
}) {
    const startLabel = evidence.start_node_labels?.join(':') ?? 'Node';
    const endLabel = evidence.end_node_labels?.join(':') ?? 'Node';
    const startProps = evidence.start_node_preview ?? {};
    const endProps = evidence.end_node_preview ?? {};
    const edgeType = (evidence.graph_edge_ref as Record<string, unknown>)?.type ?? 'RELATED_TO';

    return (
        <div className="space-y-4">
            {/* Two-node visual */}
            <div className="flex items-center gap-2" role="img" aria-label="Graph edge between two nodes">
                {/* Start node */}
                <div className="flex-1 bg-violet-950/40 border border-violet-700/50 rounded-lg p-2.5 text-center min-w-0">
                    <p className="text-[10px] uppercase tracking-wider text-violet-400 mb-1">{startLabel}</p>
                    {Object.entries(startProps).slice(0, 2).map(([k, v]) => (
                        <p key={k} className="text-xs text-gray-300 truncate">
                            <span className="text-gray-500">{k}:</span> {String(v)}
                        </p>
                    ))}
                </div>

                {/* Edge label */}
                <div className="shrink-0 text-center">
                    <div className="text-[9px] text-gray-500 mb-0.5">{String(edgeType)}</div>
                    <div className="flex items-center gap-0.5">
                        <div className="w-4 h-px bg-gray-600" />
                        <div className="w-0 h-0 border-t-4 border-b-4 border-l-4 border-t-transparent border-b-transparent border-l-gray-600" />
                    </div>
                </div>

                {/* End node */}
                <div className="flex-1 bg-violet-950/40 border border-violet-700/50 rounded-lg p-2.5 text-center min-w-0">
                    <p className="text-[10px] uppercase tracking-wider text-violet-400 mb-1">{endLabel}</p>
                    {Object.entries(endProps).slice(0, 2).map(([k, v]) => (
                        <p key={k} className="text-xs text-gray-300 truncate">
                            <span className="text-gray-500">{k}:</span> {String(v)}
                        </p>
                    ))}
                </div>
            </div>

            {/* Edge properties */}
            {evidence.graph_edge_ref && Object.keys(evidence.graph_edge_ref).length > 0 && (
                <div>
                    <p className="text-[11px] uppercase tracking-wider text-gray-500 mb-1.5">Edge properties</p>
                    <div className="bg-gray-800/60 border border-gray-700 rounded-lg p-3 text-xs space-y-0.5">
                        {Object.entries(evidence.graph_edge_ref).map(([k, v]) => (
                            <p key={k}>
                                <span className="text-gray-500">{k}:</span>{' '}
                                <span className="text-gray-300 font-mono">{JSON.stringify(v)}</span>
                            </p>
                        ))}
                    </div>
                </div>
            )}

            {/* described_in */}
            {Array.isArray(evidence.described_in) && evidence.described_in.length > 0 && (
                <div>
                    <p className="text-[11px] uppercase tracking-wider text-gray-500 mb-1.5">Described in</p>
                    <ul className="space-y-1">
                        {evidence.described_in.map((id) => (
                            <li key={id}>
                                <button
                                    type="button"
                                    onClick={() => onDescribedInClick?.(id)}
                                    className="text-xs text-amber-400 hover:text-amber-300 font-mono hover:underline focus:outline-none focus:ring-2 focus:ring-amber-500 rounded"
                                >
                                    {id}
                                </button>
                            </li>
                        ))}
                    </ul>
                </div>
            )}
        </div>
    );
}

/** map_feature — bbox coordinates + feature_properties table */
function MapFeatureRenderer({ evidence }: { evidence: MapFeatureEvidence }) {
    const bbox = evidence.bbox;
    const props = evidence.feature_properties ?? {};
    const propEntries = Object.entries(props);

    return (
        <div className="space-y-4">
            {/* Map placeholder — bbox display */}
            {bbox && (
                <div className="bg-gray-800/60 border border-gray-700 rounded-lg p-3">
                    <p className="text-[11px] uppercase tracking-wider text-gray-500 mb-2">Spatial extent (WGS84)</p>
                    <div className="grid grid-cols-2 gap-1 text-xs font-mono">
                        <div className="text-gray-500">Min lon: <span className="text-gray-300">{bbox[0].toFixed(6)}</span></div>
                        <div className="text-gray-500">Min lat: <span className="text-gray-300">{bbox[1].toFixed(6)}</span></div>
                        <div className="text-gray-500">Max lon: <span className="text-gray-300">{bbox[2].toFixed(6)}</span></div>
                        <div className="text-gray-500">Max lat: <span className="text-gray-300">{bbox[3].toFixed(6)}</span></div>
                    </div>
                    <p className="text-[10px] text-gray-600 mt-2">
                        <MapPin className="inline w-2.5 h-2.5 mr-0.5" aria-hidden="true" />
                        Open in map for interactive feature highlight
                    </p>
                </div>
            )}

            {/* tile_function */}
            {evidence.tile_function && (
                <div className="text-xs">
                    <span className="text-gray-500">Tile function: </span>
                    <span className="font-mono text-gray-300">{evidence.tile_function}</span>
                </div>
            )}

            {/* feature_properties */}
            {propEntries.length > 0 && (
                <div>
                    <p className="text-[11px] uppercase tracking-wider text-gray-500 mb-1.5">Feature properties</p>
                    <Table>
                        <TableHeader>
                            <TableRow className="border-gray-700">
                                <TableHead className="text-gray-400 text-xs w-1/3">Property</TableHead>
                                <TableHead className="text-gray-400 text-xs">Value</TableHead>
                            </TableRow>
                        </TableHeader>
                        <TableBody>
                            {propEntries.map(([key, value]) => (
                                <TableRow key={key} className="border-gray-800">
                                    <TableCell className="text-xs text-gray-400 font-mono">{key}</TableCell>
                                    <TableCell className="text-xs text-gray-300 break-all">
                                        {value === null || value === undefined
                                            ? <span className="text-gray-600">—</span>
                                            : typeof value === 'object'
                                            ? <span className="font-mono text-gray-500">{JSON.stringify(value)}</span>
                                            : String(value)}
                                    </TableCell>
                                </TableRow>
                            ))}
                        </TableBody>
                    </Table>
                </div>
            )}
        </div>
    );
}

/** Legacy path — render SSE citation payload fields when no evidence_id available */
function LegacyCitationRenderer({ citation }: { citation: Citation }) {
    return (
        <div className="space-y-3">
            <div className="bg-gray-800 border border-gray-700 rounded-lg p-3 space-y-2">
                <p className="text-sm text-gray-200 font-medium">
                    {citation.document_title || 'Unknown source'}
                </p>
                {citation.section && (
                    <p className="text-xs text-gray-400">
                        <span className="text-gray-500">Section:</span> {citation.section}
                    </p>
                )}
                {citation.page != null && (
                    <p className="text-xs text-gray-400">
                        <span className="text-gray-500">Page:</span> {citation.page}
                    </p>
                )}
                {citation.relevance_score != null && (
                    <p className="text-xs text-gray-400">
                        <span className="text-gray-500">Relevance:</span>{' '}
                        <span className="font-mono">{(citation.relevance_score * 100).toFixed(0)}%</span>
                    </p>
                )}
                {citation.source_chunk_id && (
                    <p className="text-xs text-gray-400">
                        <span className="text-gray-500">Chunk ID:</span>{' '}
                        <span className="font-mono text-gray-500 text-[10px] break-all">{citation.source_chunk_id}</span>
                    </p>
                )}
            </div>
            <div className="text-[11px] text-gray-600 space-y-0.5 border-t border-gray-800 pt-2">
                <p>
                    <span className="text-gray-500">Citation ID:</span>{' '}
                    <span className="font-mono text-gray-500">{citation.citation_id}</span>
                </p>
                <p>
                    <span className="text-gray-500">Type:</span>{' '}
                    <span className="text-gray-500">
                        {citation.citation_type === 'NI43'
                            ? 'NI 43-101 Technical Report'
                            : citation.citation_type === 'PUB'
                            ? 'Published Literature'
                            : citation.citation_type === 'PGEO'
                            ? 'Public Geoscience Record'
                            : 'Data Source'}
                    </span>
                </p>
                <p className="text-gray-600 italic mt-1 text-[10px]">
                    Full evidence details will be available once evidence records are resolved.
                </p>
            </div>
        </div>
    );
}

// ── Evidence type icons + labels ──────────────────────────────────────────

const EVIDENCE_TYPE_ICON = {
    document_passage:  { Icon: BookOpen, label: 'Document passage' },
    structured_record: { Icon: Table2,   label: 'Structured record' },
    graph_edge:        { Icon: Network,  label: 'Graph edge' },
    map_feature:       { Icon: MapPin,   label: 'Map feature' },
};

// ── EvidenceInspector ─────────────────────────────────────────────────────

export function EvidenceInspector({ open, onOpenChange, evidenceId, legacyCitation, answerRunId, workspaceId }: Props) {
    const [evidence, setEvidence] = useState<EvidencePayload | null>(null);
    const [loadState, setLoadState] = useState<'idle' | 'loading' | 'error'>('idle');
    const [errorStatus, setErrorStatus] = useState<number | 'network'>(500);
    // For described_in recursive navigation — stack of evidence ids.
    const [navStack, setNavStack] = useState<string[]>([]);

    // Effective evidence id — head of navigation stack or primary prop.
    const effectiveId = navStack.length > 0 ? navStack[navStack.length - 1] : evidenceId;

    const fetchEvidence = useCallback(async (id: string) => {
        setLoadState('loading');
        setEvidence(null);
        try {
            // Default VITE_FASTAPI_URL='/fastapi' is same-origin (nginx proxy).
            // If overridden to a full URL (e.g. http://fastapi:8000) this becomes
            // cross-origin and credentials: 'same-origin' will not send the cookie.
            // TODO(security): cross-origin call — needs service-to-service auth strategy
            // if VITE_FASTAPI_URL is set to a non-same-origin host in production.
            const fastApiBase = import.meta.env.VITE_FASTAPI_URL ?? '/fastapi';
            const res = await fetch(`${fastApiBase}/v1/evidence/${encodeURIComponent(id)}`, {
                credentials: 'same-origin',
                headers: getAuthHeaders(),
            });
            if (res.status === 404) {
                setErrorStatus(404);
                setLoadState('error');
                return;
            }
            if (!res.ok) {
                setErrorStatus(res.status === 500 ? 500 : 'network');
                setLoadState('error');
                return;
            }
            const data: EvidencePayload = await res.json();
            setEvidence(data);
            setLoadState('idle');
        } catch {
            setErrorStatus('network');
            setLoadState('error');
        }
    }, []);

    // Fetch when sheet opens with an evidenceId, or when effectiveId changes.
    useEffect(() => {
        if (!open) {
            // Reset when closed so fresh data loads on re-open.
            setEvidence(null);
            setLoadState('idle');
            setNavStack([]);
            return;
        }
        if (effectiveId) {
            fetchEvidence(effectiveId);
        }
    }, [open, effectiveId, fetchEvidence]);

    function handleDescribedInClick(id: string) {
        setNavStack((prev) => [...prev, id]);
    }

    function handleNavBack() {
        setNavStack((prev) => prev.slice(0, -1));
    }

    // Determine sheet title and icon.
    const evidenceTypeMeta = evidence?.evidence_type
        ? EVIDENCE_TYPE_ICON[evidence.evidence_type]
        : null;
    const SheetIcon = evidenceTypeMeta?.Icon ?? BookOpen;
    const sheetLabel = evidenceTypeMeta?.label ?? 'Evidence';

    const isLegacyOnly = !evidenceId && !!legacyCitation;

    // B9 — on close, return focus to the triggering element.
    // triggerRef is passed from Chat.tsx via the Props interface (optional).
    // If not provided, focus returns to document.body (Radix default).

    const SHEET_TITLE_ID = 'evidence-inspector-title';

    return (
        <Sheet open={open} onOpenChange={onOpenChange}>
            <SheetContent
                side="right"
                // B9: role="dialog" + aria-modal + aria-labelledby for WCAG 2.1 AA.
                // Radix Sheet (Dialog primitive underneath) already provides focus-trap
                // and Escape-key close — we document this here.
                // Focus-trap: Radix Dialog traps focus within SheetContent automatically.
                // Escape: Radix Dialog calls onOpenChange(false) on Escape keydown.
                role="dialog"
                aria-modal="true"
                aria-labelledby={SHEET_TITLE_ID}
                className={cn(
                    'w-[420px] sm:w-[480px] sm:max-w-[480px]',
                    'bg-gray-900 border-l border-gray-800',
                    'text-gray-100',
                    'flex flex-col p-0 gap-0',
                )}
            >
                <SheetHeader className="px-4 py-3 border-b border-gray-800 shrink-0">
                    <div className="flex items-center gap-2">
                        <SheetIcon className="w-4 h-4 text-amber-400 shrink-0" aria-hidden="true" />
                        <SheetTitle id={SHEET_TITLE_ID} className="text-sm text-gray-200">
                            {isLegacyOnly ? 'Source Reference' : sheetLabel}
                        </SheetTitle>
                        {navStack.length > 0 && (
                            <button
                                type="button"
                                onClick={handleNavBack}
                                className="ml-auto text-xs text-gray-500 hover:text-gray-300 transition-colors focus:outline-none focus:ring-2 focus:ring-amber-500 rounded px-1.5 py-0.5 border border-gray-700"
                                aria-label="Navigate back"
                            >
                                ← Back
                            </button>
                        )}
                    </div>
                    <SheetDescription className="text-xs text-gray-500 mt-0.5">
                        {isLegacyOnly
                            ? (legacyCitation?.citation_id ?? 'Citation details')
                            : (effectiveId
                                ? `Evidence ID: ${effectiveId.slice(0, 16)}…`
                                : 'No evidence ID')}
                    </SheetDescription>
                </SheetHeader>

                <ScrollArea className="flex-1">
                    <div className="px-4 py-4">
                        {/* Loading state */}
                        {loadState === 'loading' && <InspectorSkeleton />}

                        {/* Error state */}
                        {loadState === 'error' && (
                            <InspectorError
                                status={errorStatus}
                                onRetry={() => effectiveId && fetchEvidence(effectiveId)}
                            />
                        )}

                        {/* New path — type-branched evidence renderers */}
                        {loadState === 'idle' && evidence && (
                            <>
                                {evidence.evidence_type === 'document_passage' && (
                                    <DocumentPassageRenderer evidence={evidence as DocumentPassageEvidence} />
                                )}
                                {evidence.evidence_type === 'structured_record' && (
                                    <StructuredRecordRenderer evidence={evidence as StructuredRecordEvidence} />
                                )}
                                {evidence.evidence_type === 'graph_edge' && (
                                    <GraphEdgeRenderer
                                        evidence={evidence as GraphEdgeEvidence}
                                        onDescribedInClick={handleDescribedInClick}
                                    />
                                )}
                                {evidence.evidence_type === 'map_feature' && (
                                    <MapFeatureRenderer evidence={evidence as MapFeatureEvidence} />
                                )}
                            </>
                        )}

                        {/* Legacy path — SSE citation payload only */}
                        {loadState === 'idle' && !evidence && isLegacyOnly && legacyCitation && (
                            <LegacyCitationRenderer citation={legacyCitation} />
                        )}

                        {/* No data at all */}
                        {loadState === 'idle' && !evidence && !isLegacyOnly && !effectiveId && (
                            <p className="text-sm text-gray-500 text-center py-8">
                                No evidence to display.
                            </p>
                        )}

                        {/* Phase H4 §12.8 — citation feedback footer. Renders
                            when the caller threaded answerRunId + workspaceId
                            and the inspector has a citation_id + source ref. */}
                        {answerRunId && workspaceId && legacyCitation?.citation_id &&
                         (legacyCitation?.source_chunk_id || effectiveId) && (
                            <div className="mt-6 border-t border-gray-800 pt-3 flex items-center justify-between">
                                <span className="text-xs text-gray-500">
                                    Was this citation accurate?
                                </span>
                                <CitationFeedbackButtons
                                    workspaceId={workspaceId}
                                    answerRunId={answerRunId}
                                    citationItemId={legacyCitation.citation_id}
                                    sourceDocumentId={
                                        (legacyCitation.source_chunk_id ?? effectiveId) as string
                                    }
                                />
                            </div>
                        )}
                    </div>
                </ScrollArea>
            </SheetContent>
        </Sheet>
    );
}
