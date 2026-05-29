import { useState, type JSX } from 'react';
import { ThumbsUp, ThumbsDown } from 'lucide-react';

/**
 * CitationFeedbackButtons — Phase H4 §12.8.
 *
 * Per-citation 👍/👎 buttons that feed `silver.source_trust_features`
 * via POST /api/v1/citations/feedback. Renders inline next to a
 * citation (in the citation detail popover or citations panel).
 *
 * Once ≥500 feedback events accumulate per workspace, the
 * `train_source_trust` workflow can produce a real ML-trained trust
 * model (the deterministic baseline runs in the meantime).
 *
 * Optimistic UI:
 *   - Click → button highlights immediately + posts in background.
 *   - On success: toggle "thanks" state for 2s, then disable both.
 *   - On failure: rollback + show error.
 */

type Verdict = 'right' | 'wrong';

type Props = {
    workspaceId: string;
    answerRunId: string;
    citationItemId: string;
    sourceDocumentId: string;
    /** Optional callback so parent components can refresh their view */
    onSubmitted?: (verdict: Verdict, cumulative: number) => void;
};

export function CitationFeedbackButtons({
    workspaceId,
    answerRunId,
    citationItemId,
    sourceDocumentId,
    onSubmitted,
}: Props): JSX.Element {
    const [submitted, setSubmitted] = useState<Verdict | null>(null);
    const [submitting, setSubmitting] = useState<Verdict | null>(null);
    const [error, setError] = useState<string | null>(null);

    async function submit(verdict: Verdict): Promise<void> {
        if (submitted) return;
        setSubmitting(verdict);
        setError(null);
        try {
            const csrf =
                (document.querySelector('meta[name="csrf-token"]') as HTMLMetaElement | null)
                    ?.content ?? '';
            const resp = await fetch('/api/v1/citations/feedback', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'X-CSRF-TOKEN': csrf,
                },
                body: JSON.stringify({
                    workspace_id: workspaceId,
                    answer_run_id: answerRunId,
                    citation_item_id: citationItemId,
                    source_document_id: sourceDocumentId,
                    verdict,
                }),
            });
            if (resp.ok) {
                const body = await resp.json();
                setSubmitted(verdict);
                if (onSubmitted) {
                    onSubmitted(verdict, body.cumulative_feedback_for_source ?? 0);
                }
            } else {
                const body = await resp.json().catch(() => ({}));
                setError(body.error ?? 'Feedback failed.');
            }
        } catch (err) {
            setError(`Network error: ${(err as Error).message}`);
        } finally {
            setSubmitting(null);
        }
    }

    const disabledRight = submitting !== null || submitted !== null;
    const disabledWrong = submitting !== null || submitted !== null;

    return (
        <div
            className="inline-flex items-center gap-1 text-xs"
            role="group"
            aria-label="Citation feedback"
        >
            <button
                type="button"
                onClick={() => submit('right')}
                disabled={disabledRight}
                aria-label="Mark citation as right"
                title="This citation supports the claim"
                className={`p-1 rounded transition ${
                    submitted === 'right'
                        ? 'bg-green-100 text-green-700'
                        : 'text-gray-400 hover:text-green-600 hover:bg-green-50'
                } disabled:opacity-60`}
            >
                <ThumbsUp className="w-3.5 h-3.5" />
            </button>
            <button
                type="button"
                onClick={() => submit('wrong')}
                disabled={disabledWrong}
                aria-label="Mark citation as wrong"
                title="This citation does not support the claim"
                className={`p-1 rounded transition ${
                    submitted === 'wrong'
                        ? 'bg-red-100 text-red-700'
                        : 'text-gray-400 hover:text-red-600 hover:bg-red-50'
                } disabled:opacity-60`}
            >
                <ThumbsDown className="w-3.5 h-3.5" />
            </button>
            {submitted && (
                <span className="text-gray-500 ml-1">thanks</span>
            )}
            {error && (
                <span className="text-red-600 ml-1" role="alert">
                    {error}
                </span>
            )}
        </div>
    );
}

export default CitationFeedbackButtons;
