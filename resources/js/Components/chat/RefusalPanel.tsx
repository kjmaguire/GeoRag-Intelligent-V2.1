/**
 * RefusalPanel — structured refusal UX (REF-01)
 *
 * Renders when the backend returns a `refusal_payload` on a completed event,
 * or when the `failed` event arrives and we synthesise a minimal refusal shape.
 *
 * Replaces the generic "Error: …" plain-text bubble. Gives geologists:
 *   1. A clear header explaining WHY the answer was refused
 *   2. "What we searched" — stores + candidate count + query class
 *   3. "What was missing" — nearest candidates (clickable → EvidenceInspector)
 *   4. Optional failed_guards list for hallucination-guard transparency
 *   5. "Report refusal issue" button (Chunk 4 wires real feedback routing)
 *
 * Spec ref: Module 7 Phase B §B7, §10u
 */
import { AlertCircle, FileSearch, FileX, ShieldAlert } from 'lucide-react';
import { Card, CardHeader, CardTitle, CardContent, CardFooter } from '@/Components/ui/card';
import { Badge } from '@/Components/ui/badge';
import { Button } from '@/Components/ui/button';
import { Alert, AlertTitle, AlertDescription } from '@/Components/ui/alert';
import { cn } from '@/lib/utils';

// ── Types ──────────────────────────────────────────────────────────────────

export type RefusalReasonCode =
  | 'insufficient_evidence'
  | 'guard_numeric_fail'
  | 'guard_entity_fail'
  | 'guard_completeness_fail'
  | 'llm_unavailable'
  | 'budget_exhausted';

export interface NearestCandidate {
  marker: string;
  source_store: string;
  relevance_score: number;
  preview: string;
  /** May be present post-B8.5; used to route directly to EvidenceInspector */
  evidence_id?: string | null;
}

export interface RefusalPayload {
  type: 'refusal';
  reason_code: RefusalReasonCode;
  searched: {
    stores_queried: string[];
    candidates_considered: number;
    query_class: string;
  };
  missing: {
    what_was_needed: string;
    nearest_candidates: NearestCandidate[];
  };
  message: string;
  failed_guards?: string[];
}

interface Props {
  payload: RefusalPayload;
  /** Called when a nearest-candidate card is clicked. Routes to EvidenceInspector. */
  onInspectCandidate: (marker: string, evidenceId: string | null, legacyCitation: unknown) => void;
  /** Called when the "Report refusal issue" button is clicked. */
  onReportRefusalIssue: () => void;
}

// ── Header text by reason_code ─────────────────────────────────────────────

const HEADER_TEXT: Record<RefusalReasonCode, string> = {
  insufficient_evidence: "We can't answer this from your corpus",
  guard_numeric_fail: "Numbers in the draft answer don't check out",
  guard_entity_fail: "Entities in the draft answer don't match the evidence",
  guard_completeness_fail: "Not every claim was supported by evidence",
  llm_unavailable: "The language model is temporarily unavailable",
  budget_exhausted: "The query exceeded its time budget",
};

/** System-level refusals (infrastructure failures) vs grounding refusals */
const SYSTEM_LEVEL_CODES: RefusalReasonCode[] = ['llm_unavailable', 'budget_exhausted'];

function isSystemLevel(code: RefusalReasonCode): boolean {
  return SYSTEM_LEVEL_CODES.includes(code);
}

/** Source store display label: "qdrant" → "Qdrant", etc. */
function storeLabel(store: string): string {
  const MAP: Record<string, string> = {
    qdrant: 'Qdrant',
    neo4j: 'Neo4j',
    postgis: 'PostGIS',
    postgres: 'PostgreSQL',
  };
  return MAP[store.toLowerCase()] ?? store;
}

// ── NearestCandidateCard ───────────────────────────────────────────────────

interface NearestCandidateCardProps {
  candidate: NearestCandidate;
  onClick: () => void;
}

function NearestCandidateCard({ candidate, onClick }: NearestCandidateCardProps) {
  const pct = Math.round(candidate.relevance_score * 100);
  const preview =
    candidate.preview.length > 160
      ? candidate.preview.slice(0, 160).trimEnd() + '…'
      : candidate.preview;

  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'w-full text-left rounded-lg border border-gray-700 bg-gray-800/60',
        'hover:border-amber-700/60 hover:bg-gray-800 transition-colors',
        'px-3 py-2.5 space-y-1.5',
        'focus:outline-none focus:ring-2 focus:ring-amber-500 focus:ring-offset-1 focus:ring-offset-gray-900',
      )}
      aria-label={`Inspect candidate ${candidate.marker} from ${candidate.source_store}`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-mono font-medium text-amber-400">{candidate.marker}</span>
        <div className="flex items-center gap-1.5 shrink-0">
          <Badge
            variant="outline"
            className="text-[10px] border-gray-600 text-gray-400 font-normal"
          >
            {storeLabel(candidate.source_store)}
          </Badge>
          <span className="text-[10px] font-mono text-gray-500">{pct}% relevance</span>
        </div>
      </div>
      <p className="text-xs text-gray-400 leading-relaxed">{preview}</p>
    </button>
  );
}

// ── RefusalPanel ───────────────────────────────────────────────────────────

export function RefusalPanel({ payload, onInspectCandidate, onReportRefusalIssue }: Props) {
  const reasonCode = payload.reason_code;
  const headerText = HEADER_TEXT[reasonCode] ?? payload.message ?? 'Unable to answer this query';
  const sysLevel = isSystemLevel(reasonCode);

  // Top 3 candidates only
  const topCandidates = (payload.missing?.nearest_candidates ?? []).slice(0, 3);

  // System-level refusals get a prominent alert banner treatment
  // B9: role="alert" (not region) for system errors — these are urgent failures.
  // The section also has aria-labelledby via the AlertTitle h2 inside.
  if (sysLevel) {
    return (
      <div className="space-y-3" role="alert" aria-label="Query refusal details">
        <Alert
          variant="destructive"
          className="border-red-800/60 bg-red-950/30 text-red-300"
        >
          <AlertCircle className="h-4 w-4 text-red-400" aria-hidden="true" />
          <AlertTitle>
            <h2 className="text-sm font-semibold text-red-200">{headerText}</h2>
          </AlertTitle>
          <AlertDescription>
            <p className="text-xs text-red-300/80 mt-0.5">{payload.message}</p>
          </AlertDescription>
        </Alert>
        <div className="flex">
          <Button
            variant="outline"
            size="sm"
            onClick={onReportRefusalIssue}
            className="text-xs border-gray-700 text-gray-400 hover:text-gray-200"
          >
            Report refusal issue
          </Button>
        </div>
      </div>
    );
  }

  // B9: role="status" for grounding refusals (expected corpus boundary — not urgent).
  // The outer section also provides aria-label for the panel as a whole.
  return (
    <div className="space-y-3" role="status" aria-label="Query refusal details">
      <Card className="border border-gray-700 bg-gray-900/60 gap-0 py-0 shadow-none">
        {/* Header */}
        <CardHeader className="px-4 pt-4 pb-3 border-b border-gray-700/60">
          <CardTitle>
            <h2
              className="flex items-center gap-2 text-sm font-semibold text-gray-100"
              aria-label={`Refusal: ${headerText} (reason: ${reasonCode})`}
            >
              <AlertCircle
                className="h-4 w-4 text-amber-400 shrink-0"
                aria-hidden="true"
              />
              {headerText}
            </h2>
          </CardTitle>
        </CardHeader>

        <CardContent className="px-4 py-3 space-y-4">
          {/* Block 1 — What we searched */}
          <section aria-labelledby="refusal-searched-heading">
            <div className="flex items-center gap-2 mb-2">
              <FileSearch className="h-3.5 w-3.5 text-gray-500 shrink-0" aria-hidden="true" />
              <h3
                id="refusal-searched-heading"
                className="text-xs font-medium text-gray-400 uppercase tracking-wide"
              >
                What we searched
              </h3>
            </div>
            <div className="space-y-1.5 pl-5">
              {payload.searched?.query_class && (
                <div className="flex items-center gap-1.5">
                  <span className="text-xs text-gray-500">Query class:</span>
                  <Badge
                    variant="outline"
                    className="text-[10px] border-gray-600 text-gray-300"
                  >
                    {payload.searched.query_class}
                  </Badge>
                </div>
              )}
              <p className="text-xs text-gray-400">
                Searched{' '}
                <span className="font-mono text-gray-300">
                  {payload.searched?.candidates_considered ?? 0}
                </span>{' '}
                candidates across:{' '}
                <span className="text-gray-300">
                  {(payload.searched?.stores_queried ?? []).map(storeLabel).join(', ') || 'no stores'}
                </span>
              </p>
            </div>
          </section>

          {/* Block 2 — What was missing */}
          <section aria-labelledby="refusal-missing-heading">
            <div className="flex items-center gap-2 mb-2">
              <FileX className="h-3.5 w-3.5 text-gray-500 shrink-0" aria-hidden="true" />
              <h3
                id="refusal-missing-heading"
                className="text-xs font-medium text-gray-400 uppercase tracking-wide"
              >
                What was missing
              </h3>
            </div>
            <div className="space-y-2 pl-5">
              {payload.missing?.what_was_needed && (
                <p className="text-xs text-gray-300 leading-relaxed">
                  {payload.missing.what_was_needed}
                </p>
              )}

              {/* Nearest candidates — max 3, clickable */}
              {topCandidates.length > 0 && (
                <div className="space-y-1.5 mt-2">
                  <p className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
                    Nearest matches found ({topCandidates.length})
                  </p>
                  {topCandidates.map((c) => (
                    <NearestCandidateCard
                      key={c.marker}
                      candidate={c}
                      onClick={() => onInspectCandidate(c.marker, c.evidence_id ?? null, null)}
                    />
                  ))}
                </div>
              )}
            </div>
          </section>

          {/* Block 3 — Failed guards (optional, if present) */}
          {Array.isArray(payload.failed_guards) && payload.failed_guards.length > 0 && (
            <section aria-labelledby="refusal-guards-heading">
              <div className="flex items-center gap-2 mb-2">
                <ShieldAlert className="h-3.5 w-3.5 text-gray-500 shrink-0" aria-hidden="true" />
                <h3
                  id="refusal-guards-heading"
                  className="text-xs font-medium text-gray-400 uppercase tracking-wide"
                >
                  Hallucination guards triggered
                </h3>
              </div>
              <ul className="pl-5 space-y-1" role="list">
                {payload.failed_guards.map((g) => (
                  <li key={g} className="text-xs text-gray-500 font-mono">
                    {g}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </CardContent>

        {/* Footer — report action only. No thumbs-down per spec B7. */}
        <CardFooter className="px-4 pb-4 pt-0 border-t border-gray-700/60 flex items-center justify-between mt-3">
          <Button
            variant="outline"
            size="sm"
            onClick={onReportRefusalIssue}
            className="text-xs border-gray-700 text-gray-400 hover:text-gray-200"
          >
            Report refusal issue
          </Button>
          <span className="text-[10px] text-gray-600">
            No answer returned — refusal is expected behavior
          </span>
        </CardFooter>
      </Card>
    </div>
  );
}

export default RefusalPanel;
