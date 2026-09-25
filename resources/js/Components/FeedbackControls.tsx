import { useEffect, useState } from 'react';

/**
 * FeedbackControls — §10p answer feedback (chat-adjacent slice, built
 * 2026-09-24).
 *
 * 👍/👎 on a settled assistant answer. 👎 expands the 6-value taxonomy +
 * an optional free-text note, matching `silver.message_feedback`'s CHECK
 * constraints exactly (`category` is required when `polarity='down'`,
 * optional otherwise). Posts to the new
 * `POST /api/v1/answer-runs/{id}/feedback` Laravel route
 * (`AnswerRunFeedbackController`), which proxies to FastAPI's existing
 * `POST /v1/answer_runs/{id}/feedback` writer — that writer has existed
 * since 2026-04-22 with no caller anywhere in the stack (§10p as-built
 * note, georag-architecture.html).
 *
 * Multiple submissions per user per answer_run are allowed by the schema
 * ("the user can change their mind"; the UI renders the latest at render
 * time) — this component does not lock out resubmission, it just shows
 * which polarity was last recorded.
 *
 * Renders nothing when `answerRunId` is null (streaming turns, or turns
 * that errored before a run was persisted).
 */

type FeedbackCategory =
    | 'hallucinated'
    | 'wrong_facts'
    | 'missing_info'
    | 'off_topic'
    | 'citation_issue'
    | 'length_issue';

const CATEGORY_LABELS: Record<FeedbackCategory, string> = {
    hallucinated: 'Unsupported claim',
    wrong_facts: 'Wrong facts',
    citation_issue: 'Wrong citation',
    missing_info: 'Missed evidence / incomplete',
    off_topic: 'Off topic',
    length_issue: 'Too long / too short',
};

const CATEGORY_ORDER: FeedbackCategory[] = [
    'hallucinated',
    'wrong_facts',
    'citation_issue',
    'missing_info',
    'off_topic',
    'length_issue',
];

function getCsrf(): string | null {
    return document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') ?? null;
}

interface Props {
    answerRunId: string | null;
    // Set by EvidenceInspector's "Report citation issue" button (§10s
    // feedback hook) to open the down-vote form pre-filled to
    // 'citation_issue'. Bumping this to a new object reference (even with
    // the same category) re-opens the form on repeat clicks.
    presetCategory?: { category: FeedbackCategory } | null;
}

export default function FeedbackControls({ answerRunId, presetCategory }: Props) {
    const [expanded, setExpanded] = useState(false);
    const [category, setCategory] = useState<FeedbackCategory | ''>('');
    const [note, setNote] = useState('');
    const [lastPolarity, setLastPolarity] = useState<'up' | 'down' | null>(null);
    const [status, setStatus] = useState<'idle' | 'submitting' | 'error'>('idle');

    useEffect(() => {
        if (!presetCategory) return;
        setExpanded(true);
        setCategory(presetCategory.category);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [presetCategory]);

    if (!answerRunId) return null;

    async function submit(polarity: 'up' | 'down', cat: FeedbackCategory | '') {
        setStatus('submitting');
        try {
            const resp = await fetch(`/api/v1/answer-runs/${answerRunId}/feedback`, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    Accept: 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                    ...(getCsrf() ? { 'X-CSRF-TOKEN': getCsrf() as string } : {}),
                },
                body: JSON.stringify({
                    polarity,
                    category: cat || null,
                    note: note.trim() || null,
                }),
            });
            if (!resp.ok) throw new Error(`feedback failed (${resp.status})`);
            setLastPolarity(polarity);
            setStatus('idle');
            if (polarity === 'up') {
                setExpanded(false);
                setCategory('');
                setNote('');
            }
        } catch {
            setStatus('error');
        }
    }

    function handleUp() {
        void submit('up', '');
    }

    function handleDownClick() {
        setExpanded((v) => !v);
    }

    function handleDownSubmit(e: React.FormEvent) {
        e.preventDefault();
        if (!category) return;
        void submit('down', category);
    }

    const pillBase = 'font-mono text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded border';

    return (
        <div className="mt-1.5" data-testid="feedback-controls">
            <div className="flex items-center gap-1.5">
                <button
                    type="button"
                    onClick={handleUp}
                    aria-label="Good answer"
                    aria-pressed={lastPolarity === 'up'}
                    className={pillBase}
                    style={{
                        color: lastPolarity === 'up' ? 'var(--accent)' : 'var(--fg-3)',
                        borderColor: lastPolarity === 'up' ? 'var(--accent)' : 'var(--line-2)',
                        background: 'transparent',
                    }}
                >
                    👍
                </button>
                <button
                    type="button"
                    onClick={handleDownClick}
                    aria-label="Bad answer"
                    aria-pressed={lastPolarity === 'down'}
                    aria-expanded={expanded}
                    className={pillBase}
                    style={{
                        color: lastPolarity === 'down' ? 'var(--warn, #d97706)' : 'var(--fg-3)',
                        borderColor: lastPolarity === 'down' ? 'var(--warn, #d97706)' : 'var(--line-2)',
                        background: 'transparent',
                    }}
                >
                    👎
                </button>
                {status === 'error' && (
                    <span className="text-[10px] font-mono" style={{ color: 'var(--danger, #ef4444)' }} role="alert">
                        Feedback failed to send.
                    </span>
                )}
            </div>
            {expanded && (
                <form
                    onSubmit={handleDownSubmit}
                    className="mt-1.5 rounded-md border px-3 py-2 text-xs space-y-2"
                    style={{ borderColor: 'var(--line-1)', background: 'var(--bg-2)' }}
                >
                    <select
                        value={category}
                        onChange={(e) => setCategory(e.target.value as FeedbackCategory)}
                        aria-label="Feedback category"
                        required
                        className="w-full text-xs px-2 py-1 rounded border"
                        style={{ background: 'var(--bg-1)', color: 'var(--fg-1)', borderColor: 'var(--line-2)' }}
                    >
                        <option value="" disabled>
                            Select a reason…
                        </option>
                        {CATEGORY_ORDER.map((c) => (
                            <option key={c} value={c}>
                                {CATEGORY_LABELS[c]}
                            </option>
                        ))}
                    </select>
                    <textarea
                        value={note}
                        onChange={(e) => setNote(e.target.value)}
                        placeholder="Optional note…"
                        aria-label="Feedback note"
                        rows={2}
                        maxLength={2000}
                        className="w-full text-xs px-2 py-1 rounded border resize-none"
                        style={{ background: 'var(--bg-1)', color: 'var(--fg-1)', borderColor: 'var(--line-2)' }}
                    />
                    <button
                        type="submit"
                        disabled={!category || status === 'submitting'}
                        className={pillBase + ' disabled:opacity-40'}
                        style={{ color: 'var(--warn, #d97706)', borderColor: 'var(--warn, #d97706)', background: 'transparent' }}
                    >
                        {status === 'submitting' ? 'Sending…' : 'Submit feedback'}
                    </button>
                </form>
            )}
        </div>
    );
}
