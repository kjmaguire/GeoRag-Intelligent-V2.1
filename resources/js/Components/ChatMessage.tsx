// @ts-nocheck — migration in progress, will add full type annotations incrementally
import { memo, useEffect, useRef, useState } from "react";
import { useState as useLocalState } from 'react';
import { createPortal } from 'react-dom';
import Markdown from 'react-markdown';
import { RotateCcw } from 'lucide-react';
import InlineViz from './InlineViz';
import { CitationMarker } from './chat/CitationMarker';
import { RefusalPanel } from './chat/RefusalPanel';
// Plan §4b/§4d — typed guard error surfaces. Renders below the message
// bubble when the assistant response carries `guard_error_codes` (from
// FastAPI's GeoRAGResponse, or from chat_messages.metadata for
// historical messages). RefusalPanel above still handles the rejected
// state — this is supplementary for partial / ambiguity / conflict /
// incident surfaces that the legacy refusal path doesn't cover.
import { GuardErrorDispatcher } from './GuardError';
import { FeedbackButtons } from './chat/FeedbackButtons';
import { ConflictCards } from './chat/ConflictCards';
import { FreshnessBadge } from './chat/FreshnessBadge';
import { Skeleton } from './ui/skeleton';
import { Button } from './ui/button';
import type { MarkerKind } from './chat/CitationMarker';
import type { LifecycleState, RefusalPayload } from '@/types';
// Phase G.4 — Evidence Map Mode: clicking a spatial citation marker
// pins the underlying feature on MapView via a shared store. The map
// surface subscribes via `useEvidenceMapPin`.
import { parseSpatialCitation } from '@/lib/spatialCitation';
import { setEvidenceMapPin } from '@/Hooks/useEvidenceMapPin';

/**
 * ChatMessage
 *
 * Renders a single message from either the user or the assistant.
 *
 * Citation formats parsed from text:
 *   [NI43-X]   NI 43-101 report reference       -> amber/orange badge
 *   [PUB-X]    Published literature              -> blue badge
 *   [DATA-X]   Data source / assay file          -> green badge
 *   [PGEO-X]   Public Geoscience (government)    -> copper/red badge
 *
 * Where X is one or more alphanumeric characters / hyphens.
 *
 * M2 Phase 5 — visualizations:
 *   Assistant messages may include `mapPayload` (GeoJSON FeatureCollection of
 *   drill collars) and/or `vizPayload` (chart hint). When present, these
 *   render below the bubble via <InlineViz>. The viz panel is collapsible
 *   and the data comes from the FastAPI "completed" SSE event — see
 *   src/fastapi/app/agent/viz_builder.py.
 */

/**
 * Unified citation regex — accepts BOTH separator forms for forward/backward
 * compatibility:
 *   Dash-form  (pre-Module-6): [NI43-N], [PUB-N], [DATA-N], [PGEO-N]
 *   Colon-form (Module 6+):    [NI43:N], [PUB:N], [DATA:N], [PGEO:N]
 *   Evidence-id form (future): [ev:<uuid>]
 *
 * Groups:
 *   match[1] — kind  (NI43 | PUB | DATA | PGEO | ev)
 *   match[2] — id    (numeric index, alphanumeric slug, or UUID string)
 *
 * This is the single source of truth for citation matching.
 * Keep in sync with the regex coverage test in __tests__/CitationMarker.test.tsx.
 */
export const CITATION_RE = /\[(NI43|PUB|DATA|PGEO|ev)[-:]([A-Za-z0-9-]+)\]/g;

/**
 * Parse message text into plain strings and citation descriptor objects.
 * Returns {type, raw, kind, id} for each citation match.
 */
function parseSegments(text) {
    const segments = [];
    let lastIndex = 0;
    let match;

    CITATION_RE.lastIndex = 0;

    while ((match = CITATION_RE.exec(text)) !== null) {
        if (match.index > lastIndex) {
            segments.push(text.slice(lastIndex, match.index));
        }
        segments.push({
            type: 'citation',
            raw:  match[0],
            kind: match[1],
            id:   match[2],
        });
        lastIndex = CITATION_RE.lastIndex;
    }

    if (lastIndex < text.length) {
        segments.push(text.slice(lastIndex));
    }

    return segments;
}

/**
 * CitationChip — thin adapter that wraps CitationMarker with the hovercard
 * portal for preview-on-hover. The CitationMarker handles icon + click;
 * this component adds the tooltip preview from the SSE citation cache.
 *
 * CITE-01 fix: now uses CITATION_RE with colon-form support (both separators).
 * CITE-02/03/04: delegates icon selection to CitationMarker per kind + evidence_type.
 */
function CitationChip({ raw, kind, id, onCitationClick, citations }) {
    const buttonRef = useRef(null);
    const [hoverState, setHoverState] = useState({ open: false, top: 0, left: 0 });

    // Resolve citation metadata from the per-message SSE cache.
    // Match on citation_id using the raw marker string (legacy dash-form cache
    // entries use `[KIND-N]`; colon-form entries use `[KIND:N]`).
    const citData = Array.isArray(citations)
        ? citations.find((c) => c?.citation_id === raw)
        : null;

    const openCard = () => {
        if (!citData || !buttonRef.current) return;
        const rect = buttonRef.current.getBoundingClientRect();
        const cardWidth = 256;
        let left = rect.left + rect.width / 2 - cardWidth / 2;
        left = Math.max(8, Math.min(left, window.innerWidth - cardWidth - 8));
        setHoverState({ open: true, top: rect.top - 8, left });
    };
    const closeCard = () => setHoverState((s) => ({ ...s, open: false }));

    useEffect(() => {
        if (!hoverState.open) return;
        const close = () => closeCard();
        window.addEventListener('scroll', close, true);
        window.addEventListener('resize', close);
        return () => {
            window.removeEventListener('scroll', close, true);
            window.removeEventListener('resize', close);
        };
    }, [hoverState.open]);

    const cardId = `citation-preview-${raw}`;

    return (
        <span
            ref={buttonRef}
            onMouseEnter={openCard}
            onMouseLeave={closeCard}
            onFocus={openCard}
            onBlur={closeCard}
            className="inline-block"
            aria-describedby={citData ? cardId : undefined}
        >
            <CitationMarker
                kind={kind}
                id={id}
                citation={citData}
                onClick={(cit, _k, _i) => {
                    // Phase G.4 — when the cited evidence is spatial,
                    // also pin it on MapView. The existing inspector
                    // open behaviour fires either way.
                    const pin = parseSpatialCitation(cit);
                    if (pin) setEvidenceMapPin(pin);
                    onCitationClick?.(raw);
                }}
            />
            {citData && hoverState.open && typeof document !== 'undefined' && createPortal(
                <div
                    id={cardId}
                    role="tooltip"
                    style={{
                        position: 'fixed',
                        top: hoverState.top,
                        left: hoverState.left,
                        width: 256,
                        transform: 'translateY(-100%)',
                    }}
                    className="z-[9999] bg-gray-900 border border-gray-700 rounded-lg shadow-xl p-2.5 text-left pointer-events-none"
                >
                    <span className="block text-xs font-medium text-gray-200 truncate mb-0.5">
                        {citData.document_title || 'Source document'}
                    </span>
                    {citData.section && (
                        <span className="block text-[11px] text-gray-400 truncate">
                            {citData.section}
                        </span>
                    )}
                    {citData.page != null && (
                        <span className="block text-[11px] text-gray-500">
                            Page {citData.page}
                        </span>
                    )}
                    {citData.relevance_score != null && (
                        <span className="block text-[11px] text-gray-500 font-mono mt-1">
                            Relevance: {(citData.relevance_score * 100).toFixed(0)}%
                        </span>
                    )}
                    <span className="block text-[10px] text-amber-400 mt-1">Click for full source →</span>
                </div>,
                document.body,
            )}
        </span>
    );
}

function MessageContent({ text, onCitationClick, citations }) {
    const segments = parseSegments(text);

    return (
        <div className="leading-relaxed break-words prose prose-invert prose-sm max-w-none
                        prose-p:my-1 prose-li:my-0.5 prose-headings:text-gray-200
                        prose-code:bg-gray-700 prose-code:px-1 prose-code:rounded prose-code:text-amber-300
                        prose-pre:bg-gray-800 prose-pre:border prose-pre:border-gray-700 prose-pre:rounded-lg">
            {segments.map((segment, i) => {
                if (typeof segment === 'string') {
                    // Render plain text segments through Markdown
                    return segment ? (
                        <Markdown key={i} components={{
                            // Keep paragraphs inline-friendly
                            p: ({ children }) => <span>{children} </span>,
                        }}>
                            {segment}
                        </Markdown>
                    ) : null;
                }
                // Citation segment — pass kind + id for type-branched icon
                return (
                    <CitationChip
                        key={i}
                        raw={segment.raw}
                        kind={segment.kind}
                        id={segment.id}
                        onCitationClick={onCitationClick}
                        citations={citations}
                    />
                );
            })}
        </div>
    );
}

/**
 * C1 + R16 — Phase checklist rendering with post-completion toggle.
 *
 * While streaming (isComplete=false): always visible.
 * After completion (isComplete=true): hidden behind a "Show trail" toggle
 *   button that expands the whole list read-only. One trail per message;
 *   state lives in the child so it doesn't survive re-renders from the
 *   parent.
 */
function PhaseChecklist({ phases, isComplete, isUser }) {
    const [expanded, setExpanded] = useLocalState(false);
    if (isUser) return null;
    if (!Array.isArray(phases) || phases.length === 0) return null;

    const list = (
        <ul
            className="px-1 mt-1.5 space-y-1 text-xs"
            role="list"
            aria-live={isComplete ? undefined : 'polite'}
            aria-label="Query processing steps"
        >
            {phases.map((phase, i) => (
                <li key={i} className="flex items-center gap-1.5 text-gray-400">
                    {phase.state === 'running' ? (
                        <span
                            className="w-3 h-3 rounded-full border-2 border-gray-600 border-t-amber-400 animate-spin motion-reduce:animate-none shrink-0"
                            aria-hidden="true"
                        />
                    ) : (
                        <svg
                            xmlns="http://www.w3.org/2000/svg"
                            viewBox="0 0 24 24"
                            fill="currentColor"
                            className="w-3.5 h-3.5 text-emerald-500 shrink-0"
                            aria-hidden="true"
                        >
                            <path fillRule="evenodd" d="M19.916 4.626a.75.75 0 0 1 .208 1.04l-9 13.5a.75.75 0 0 1-1.154.114l-6-6a.75.75 0 0 1 1.06-1.06l5.353 5.353 8.493-12.74a.75.75 0 0 1 1.04-.207Z" clipRule="evenodd" />
                        </svg>
                    )}
                    <span className={phase.state === 'done' ? 'text-gray-500' : 'text-gray-300'}>
                        {phase.label}
                    </span>
                    {phase.state === 'done' && (
                        <span className="sr-only">completed</span>
                    )}
                </li>
            ))}
        </ul>
    );

    // Streaming: show the list.
    if (!isComplete) return list;

    // Post-completion: collapsed behind a toggle.
    const doneCount = phases.filter((p) => p.state === 'done').length;
    return (
        <div className="px-1 mt-1.5">
            <button
                type="button"
                onClick={() => setExpanded((v) => !v)}
                className="text-[11px] text-gray-500 hover:text-amber-400 focus:outline-none focus:text-amber-400 inline-flex items-center gap-1 transition-colors"
                aria-expanded={expanded}
                aria-controls={`trail-${phases.length}-${doneCount}`}
            >
                <svg
                    xmlns="http://www.w3.org/2000/svg"
                    viewBox="0 0 24 24"
                    fill="currentColor"
                    className={`w-3 h-3 transition-transform ${expanded ? 'rotate-90' : ''}`}
                    aria-hidden="true"
                >
                    <path fillRule="evenodd" d="M8.22 5.22a.75.75 0 0 1 1.06 0l6.25 6.25a.75.75 0 0 1 0 1.06l-6.25 6.25a.75.75 0 0 1-1.06-1.06L13.94 12 8.22 6.28a.75.75 0 0 1 0-1.06Z" clipRule="evenodd" />
                </svg>
                {expanded ? 'Hide trail' : `Show trail · ${doneCount} step${doneCount === 1 ? '' : 's'}`}
            </button>
            {expanded && (
                <div id={`trail-${phases.length}-${doneCount}`}>{list}</div>
            )}
        </div>
    );
}


function CopyButton({ text }) {
    const [copied, setCopied] = useLocalState(false);

    function handleCopy() {
        navigator.clipboard.writeText(text).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        });
    }

    return (
        <button
            type="button"
            onClick={handleCopy}
            className="text-[10px] text-gray-600 hover:text-gray-300 px-1 py-0.5 rounded transition-colors"
            title="Copy message"
            aria-label="Copy message text to clipboard"
        >
            {copied ? '✓ Copied' : 'Copy'}
        </button>
    );
}

function ConfidenceIndicator({ score }) {
    if (score == null) return null;

    const pct = Math.round(score * 100);

    let colorClass;
    let label;
    if (pct >= 80) {
        colorClass = 'bg-green-500';
        label = 'High — well-grounded in source data';
    } else if (pct >= 50) {
        colorClass = 'bg-amber-500';
        label = 'Medium — partially grounded, verify key claims';
    } else {
        colorClass = 'bg-red-500';
        label = 'Low — limited source data, treat with caution';
    }

    // C9 — keyboard accessibility: the tooltip was `group-hover:block` only,
    // which meant keyboard users never saw the high/medium/low explanation.
    // The bar is now tab-focusable and the tooltip shows on focus-within too.
    return (
        <div
            className="flex items-center gap-1.5 mt-1.5 group relative"
            tabIndex={0}
            role="group"
            aria-label={`Response confidence ${pct} percent — ${label}`}
        >
            <div className="w-16 h-1 bg-gray-700 rounded-full overflow-hidden">
                <div
                    className={'h-full rounded-full ' + colorClass + ' transition-all duration-500'}
                    style={{ width: pct + '%' }}
                />
            </div>
            <span className="text-xs text-gray-400 font-mono">{pct}%</span>
            {/* Tooltip — visible on hover AND on keyboard focus. Bumped from
                10px to 11px for WCAG AA minimum size on tooltip text. */}
            <span
                role="tooltip"
                className="hidden group-hover:block group-focus-within:block absolute bottom-full left-0 mb-1 px-2 py-1 text-[11px] text-gray-200 bg-gray-800 border border-gray-700 rounded shadow-lg whitespace-nowrap z-50"
            >
                {label}
            </span>
        </div>
    );
}

// ── Follow-up chip omit rules (FLUP) ─────────────────────────────────────
//
// Module 7 Phase B §B5: chips must be omitted in these conditions.

const CHIP_CONFIDENCE_THRESHOLD = 0.25;

/**
 * Determine whether follow-up suggestion chips should be rendered for a
 * given message. All four omit rules must pass for chips to appear.
 *
 * Rule 1: rejected messages never show chips (spec B5 — refusal state).
 * Rule 2: no chips available.
 * Rule 3: confidence floor — low-confidence answers produce unreliable chips.
 * Rule 4: workspace-level hide preference.
 *         Workspace-level enable/disable for follow-up chips is deferred until
 *         workspace_settings ships; see ops/backlog/v1.5-followups.md.
 */
export function shouldRenderFollowups(message: {
  lifecycle_state?: LifecycleState;
  followups?: string[];
  confidence?: number | null;
}): boolean {
  // Rule 1: rejected messages never show chips
  if (message.lifecycle_state === 'rejected') return false;
  // Rule 2: no chips available
  if (!message.followups || message.followups.length === 0) return false;
  // Rule 3: confidence floor
  if (message.confidence != null && message.confidence < CHIP_CONFIDENCE_THRESHOLD) return false;
  // Rule 4: workspace preference — deferred until workspace_settings ships; always passes for now.
  return true;
}

// ── Lifecycle visual helpers (B2) ─────────────────────────────────────────

/**
 * Resolve effective lifecycle state. Falls back to 'committed' for legacy
 * messages loaded from localStorage before Module 7 Chunk 3, so the visual
 * is always in a valid state.
 */
function resolveLifecycle(message: { lifecycle_state?: LifecycleState; confidence?: number | null }): LifecycleState {
  if (message.lifecycle_state) return message.lifecycle_state;
  // Legacy backward compat: message with confidence = completed and committed
  return 'committed';
}

/**
 * Skeleton citation chip for draft/generated states.
 * Mimics the size of a real CitationMarker chip.
 */
function SkeletonCitationChip() {
  return <Skeleton className="inline-block h-5 w-12 rounded-full align-middle mx-0.5" />;
}

/**
 * "Validating…" transient badge shown in the generated state.
 */
function ValidatingBadge() {
  return (
    <span
      className="inline-flex items-center gap-1 text-[10px] text-gray-500 border border-gray-700 rounded-full px-2 py-0.5 ml-2"
      aria-live="polite"
      aria-label="Validating response"
    >
      <span
        className="w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse"
        aria-hidden="true"
      />
      Validating…
    </span>
  );
}

// FeedbackButtons is now imported from './chat/FeedbackButtons' (B6 — Chunk 4).
// The inline placeholder has been removed.

/**
 * ChatMessage
 *
 * Props:
 *   message: {
 *     id: string | number,
 *     role: 'user' | 'assistant',
 *     content: string,
 *     confidence?: number,           // 0.0 to 1.0, assistant only
 *     timestamp?: string | Date,
 *     mapPayload?: object|null,      // FastAPI MapPayload (M2 P5)
 *     vizPayload?: object|null,      // FastAPI VizPayload (M2 P5)
 *     lifecycle_state?: LifecycleState,  // Module 7 §B2
 *     refusal_payload?: RefusalPayload|null, // Module 7 §B7
 *   }
 *   projectId?: string           // Active project UUID (needed for StripLogViewer API fetch)
 *   onCitationClick?: (citationRaw: string) => void
 *   onRegenerate?: (assistantMessageId: string) => void  // retry/regenerate callback
 *   onFollowupClick?: (query: string) => void             // D3 — follow-up suggestion chip click
 *   onInspectCandidate?: (marker, evidenceId, legacy) => void  // Module 7 §B7 refusal candidate
 *   isStreaming?: boolean        // True while any stream is in flight for this thread
 */
interface ChatMessageProps {
    message: any;
    projectId?: any;
    onCitationClick?: any;
    onRegenerate?: any;
    onFollowupClick?: any;
    onInspectCandidate?: any;
    isStreaming?: any;
}

function ChatMessage({ message, projectId, onCitationClick, onRegenerate, onFollowupClick, onInspectCandidate, isStreaming }: ChatMessageProps) {
    const isUser = message.role === 'user';
    const isAssistantComplete = !isUser && message.confidence != null && !message.error;
    const hasError = !isUser && Boolean(message.error);
    const isLowConfidence = !isUser && message.confidence != null && message.confidence < 0.5;

    // Module 7 §B2 — resolved lifecycle state
    const lifecycle = isUser ? 'committed' : resolveLifecycle(message);
    const isRejected = lifecycle === 'rejected';
    const isDraft = lifecycle === 'draft';
    const isGenerated = lifecycle === 'generated';
    const isValidatedOrCommitted = lifecycle === 'validated' || lifecycle === 'committed';

    const timestamp = message.timestamp
        ? new Date(message.timestamp).toLocaleTimeString([], {
              hour: '2-digit',
              minute: '2-digit',
          })
        : null;

    return (
        <div
            className={'flex w-full ' + (isUser ? 'justify-end' : 'justify-start') + ' mb-4'}
            data-message-id={message.id}
            data-role={message.role}
        >
            {/* Assistant avatar */}
            {!isUser && (
                <div
                    className="w-7 h-7 rounded-full bg-amber-700 flex items-center justify-center text-xs font-bold text-amber-100 shrink-0 mt-0.5 mr-2"
                    aria-hidden="true"
                >
                    G
                </div>
            )}

            <div className={'flex flex-col ' + (isUser ? 'items-end' : 'items-start') + ' max-w-[90%] sm:max-w-[75%]'}>
                {/* Role label + timestamp. C9 — bumped gray-500/600 to
                    gray-400/500 so the primary role label hits WCAG AA on
                    the gray-900 background. */}
                <span className="text-xs text-gray-400 mb-1 px-1 flex items-center">
                    {isUser ? 'You' : 'GeoRAG'}
                    {timestamp && (
                        <span className="ml-2 text-gray-500">{timestamp}</span>
                    )}
                    {/* Module 7 §B2 — transient "Validating…" badge in generated state */}
                    {!isUser && isGenerated && <ValidatingBadge />}
                </span>

                {/* Message bubble — §B2: rejected state renders RefusalPanel instead of content */}
                {!isUser && isRejected && message.refusal_payload ? (
                    <div className="w-full">
                        <RefusalPanel
                            payload={message.refusal_payload}
                            onInspectCandidate={onInspectCandidate ?? (() => {})}
                            onReportRefusalIssue={() => {
                                // Chunk 4: refusal issues are reported via FeedbackButtons
                                // which is hidden on rejected messages; this handler remains
                                // as an escape hatch from inside RefusalPanel's footer button.
                                console.log('[GeoRAG] Report refusal issue', {
                                    messageId: message.id,
                                    reasonCode: message.refusal_payload?.reason_code,
                                });
                            }}
                        />
                    </div>
                ) : (
                <div
                    className={
                        'rounded-2xl px-4 py-3 text-sm ' +
                        (isUser
                            ? 'bg-blue-600 text-white rounded-br-sm'
                            : 'bg-gray-800 text-gray-100 border border-gray-700 rounded-bl-sm')
                    }
                >
                    {/* §B2 draft state: show skeleton chips instead of parsed citations.
                        B9: aria-live="polite" + aria-atomic="false" so screen readers
                        announce incremental tokens without re-reading the whole bubble. */}
                    {!isUser && isDraft ? (
                        <div
                            className="leading-relaxed"
                            aria-live="polite"
                            aria-atomic="false"
                            aria-label="Streaming response"
                        >
                            <MessageContent
                                text={message.content}
                                onCitationClick={onCitationClick}
                                citations={[]}
                            />
                            {/* Typing indicator when draft content is empty */}
                            {!message.content && (
                                <span className="text-xs text-gray-500 italic">
                                    Thinking…
                                </span>
                            )}
                        </div>
                    ) : (
                        <MessageContent
                            text={message.content}
                            onCitationClick={onCitationClick}
                            citations={message.citations}
                        />
                    )}
                </div>
                )}

                {/* Plan §4b/§4d — guard error surfaces. Renders only for
                    assistant messages NOT already in the rejected state
                    (RefusalPanel above handles those). Codes come from
                    either the live SSE payload (`message.guard_error_codes`)
                    or the persisted metadata for historical messages
                    (`message.metadata?.guard_error_codes`). */}
                {!isUser && !isRejected && (() => {
                    const codes = (
                        Array.isArray(message.guard_error_codes)
                            ? message.guard_error_codes
                            : (Array.isArray(message.metadata?.guard_error_codes)
                                ? message.metadata.guard_error_codes
                                : [])
                    ).filter(c => typeof c === 'string' && c.length > 0);
                    if (codes.length === 0) {
                        return null;
                    }
                    return (
                        <div className="mt-2 space-y-2" data-testid="guard-error-surfaces">
                            {codes.map((code) => (
                                <GuardErrorDispatcher
                                    key={code}
                                    code={code}
                                    placeholders={message.guard_error_placeholders ?? {}}
                                />
                            ))}
                        </div>
                    );
                })()}

                {/* C1 + R16 — accumulating phase checklist.
                    During streaming (no confidence yet): always visible —
                    "proof of work" converts the wait into a visible step-list.
                    After completion (confidence set): collapsed behind a
                    "Show trail" toggle per message. Default collapsed matches
                    Claude's clean post-answer surface; one click gives
                    Perplexity-style retrospective transparency for anyone who
                    wants to see which models/steps ran. */}
                <PhaseChecklist phases={message.phases} isComplete={!!message.confidence} isUser={isUser} />
                {/* Legacy single-line status fallback for the brief moment
                    before the first `status` event arrives (no phases yet).
                    Hides once the checklist has items OR once completion lands. */}
                {!isUser && message.status && !message.confidence && !(Array.isArray(message.phases) && message.phases.length > 0) && (
                    <div className="px-1 mt-1 flex items-center gap-1.5">
                        <div className="w-3 h-3 rounded-full border-2 border-gray-600 border-t-amber-400 animate-spin motion-reduce:animate-none" />
                        <span className="text-xs text-gray-400">{message.status}</span>
                    </div>
                )}

                {/* C7 — degraded-sources warning. Rendered above the
                    confidence bar so users see "partial answer — X source
                    timed out" BEFORE the answer's confidence chip lulls
                    them into trusting it. Only on completed assistant
                    messages (confidence present) with at least one
                    degraded source. */}
                {!isUser && message.confidence != null && Array.isArray(message.degradedSources) && message.degradedSources.length > 0 && (
                    <div
                        className="px-1 mt-1.5 flex items-start gap-1.5 text-xs bg-amber-950/40 border border-amber-800/50 rounded px-2 py-1"
                        role="status"
                        aria-label="Degraded sources warning"
                    >
                        <svg
                            xmlns="http://www.w3.org/2000/svg"
                            viewBox="0 0 24 24"
                            fill="currentColor"
                            className="w-3.5 h-3.5 text-amber-400 shrink-0 mt-0.5"
                            aria-hidden="true"
                        >
                            <path fillRule="evenodd" d="M9.401 3.003c1.155-2 4.043-2 5.197 0l7.355 12.748c1.154 2-.29 4.5-2.599 4.5H4.645c-2.309 0-3.752-2.5-2.598-4.5L9.4 3.003ZM12 8.25a.75.75 0 0 1 .75.75v3.75a.75.75 0 0 1-1.5 0V9a.75.75 0 0 1 .75-.75Zm0 8.25a.75.75 0 1 0 0-1.5.75.75 0 0 0 0 1.5Z" clipRule="evenodd" />
                        </svg>
                        <span className="text-amber-200">
                            <span className="font-medium">Partial answer</span> — {message.degradedSources.length === 1 ? 'one source was' : `${message.degradedSources.length} sources were`} unavailable:
                            <span className="text-amber-300 font-mono ml-1">
                                {message.degradedSources.join(', ')}
                            </span>
                        </span>
                    </div>
                )}

                {/* Confidence indicator + copy button + freshness badge on assistant messages */}
                {!isUser && (
                    <div className="px-1 flex items-center gap-2 flex-wrap">
                        <ConfidenceIndicator score={message.confidence} />
                        {/* B8 — FreshnessBadge: inline pill next to confidence bar */}
                        {isValidatedOrCommitted && message.freshness && (
                            <FreshnessBadge freshness={message.freshness} />
                        )}
                        {message.content && <CopyButton text={message.content} />}

                        {/* Regenerate/Retry control.
                            Two visual modes share one onRegenerate handler:
                              - "Retry" pill for errored or low-confidence answers
                                (foregrounds recovery when the answer is likely bad).
                              - Subtle icon for normal successful answers
                                (matches Claude/ChatGPT parity).
                            Hidden while any stream is in flight or while this
                            bubble is still streaming its first token. */}
                        {onRegenerate && !isStreaming && !message.status && (hasError || message.confidence != null) && (
                            hasError || isLowConfidence ? (
                                <button
                                    type="button"
                                    onClick={() => onRegenerate(message.id)}
                                    className="text-[10px] text-gray-500 hover:text-amber-400 border border-gray-700 hover:border-amber-700 rounded px-1.5 py-0.5 transition-colors"
                                    title={hasError ? 'Retry this query' : 'Regenerate — low confidence answer'}
                                    aria-label={hasError ? 'Retry this query' : 'Regenerate answer with low confidence'}
                                >
                                    Retry
                                </button>
                            ) : isAssistantComplete ? (
                                <button
                                    type="button"
                                    onClick={() => onRegenerate(message.id)}
                                    className="text-gray-600 hover:text-amber-400 p-1 rounded transition-colors focus:outline-none focus:ring-2 focus:ring-amber-500"
                                    title="Regenerate this answer"
                                    aria-label="Regenerate this answer"
                                >
                                    <RotateCcw className="w-3 h-3" aria-hidden="true" />
                                </button>
                            ) : null
                        )}
                    </div>
                )}

                {/* Module 7 §B8 — conflict cards: below lifecycle visuals, above follow-up chips.
                    Renders only when conflicting_evidence is non-empty. */}
                {!isUser && isValidatedOrCommitted && Array.isArray(message.conflicting_evidence) && message.conflicting_evidence.length > 0 && (
                    <ConflictCards
                        conflicts={message.conflicting_evidence}
                        onInspectEvidence={(evidenceId) => {
                            // Open EvidenceInspector for the conflict evidence chip
                            onInspectCandidate?.('[ev:' + evidenceId + ']', evidenceId, null);
                        }}
                    />
                )}

                {/* Module 7 §B6 — real feedback buttons on validated/committed states.
                    NOT shown on rejected (refusal) messages per spec B7.
                    Chunk 4: passes answer_run_id for POST /v1/answer_runs/{id}/feedback. */}
                {!isUser && isValidatedOrCommitted && !isStreaming && (
                    <FeedbackButtons
                        answerRunId={message.answer_run_id}
                        isStreaming={isStreaming}
                    />
                )}

                {/* D3 — follow-up suggestion chips.
                    Module 7 §B5: gated through shouldRenderFollowups() which enforces 4 omit rules:
                      1. No chips on rejected (refusal) messages
                      2. No chips when followups array is empty
                      3. No chips when confidence < 0.25 (low-confidence answer)
                      4. Workspace preference (workspace_settings.followup_chips_enabled) —
                         deferred until workspace_settings ships; see ops/backlog/v1.5-followups.md.
                    Disabled while another stream is in flight (isStreaming). */}
                {!isUser
                  && shouldRenderFollowups(message)
                  && onFollowupClick
                  && (
                    <div className="w-full mt-2">
                        <p className="text-[10px] uppercase tracking-wide text-gray-500 mb-1.5">Explore deeper</p>
                        <div className="flex flex-wrap gap-1.5">
                            {message.followups.map((suggestion, i) => (
                                <button
                                    key={i}
                                    type="button"
                                    onClick={() => onFollowupClick(suggestion)}
                                    disabled={isStreaming}
                                    className={[
                                        'text-left text-xs',
                                        'text-amber-300 hover:text-amber-200',
                                        'bg-amber-950/30 hover:bg-amber-900/40',
                                        'border border-amber-800/50 hover:border-amber-600/50',
                                        'rounded-full px-3 py-1',
                                        'transition-colors',
                                        'focus:outline-none focus:ring-2 focus:ring-amber-500',
                                        'disabled:opacity-40 disabled:cursor-not-allowed',
                                        'max-w-sm',
                                    ].join(' ')}
                                    aria-label={`Send follow-up query: ${suggestion}`}
                                >
                                    {suggestion}
                                </button>
                            ))}
                        </div>
                    </div>
                )}

                {/* Inline visualization panel — maps and charts derived from tool results */}
                {!isUser && (message.mapPayload || message.vizPayload) && (
                    <div className="w-full mt-1">
                        <InlineViz
                            mapPayload={message.mapPayload}
                            vizPayload={message.vizPayload}
                            projectId={projectId}
                        />
                    </div>
                )}
            </div>

            {/* User avatar */}
            {isUser && (
                <div
                    className="w-7 h-7 rounded-full bg-blue-700 flex items-center justify-center text-xs font-bold text-blue-100 shrink-0 mt-0.5 ml-2"
                    aria-hidden="true"
                >
                    U
                </div>
            )}
        </div>
    );
}


export default memo(ChatMessage);
