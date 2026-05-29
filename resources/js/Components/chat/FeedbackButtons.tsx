// @ts-nocheck — migration in progress
/**
 * FeedbackButtons — B6
 *
 * Thumbs-up / thumbs-down per completed assistant bubble.
 *
 * Spec §10p taxonomy (6 categories, required when polarity is 'down'):
 *   hallucinated   — model invented / fabricated facts
 *   wrong_facts    — factually incorrect but plausible
 *   missing_info   — incomplete / missing needed context
 *   off_topic      — not relevant to the question
 *   citation_issue — wrong, missing, or mis-matched citations
 *   length_issue   — answer too long or too short
 *
 * Hides on:
 *   - streaming (isStreaming=true)
 *   - refusal/rejected state (lifecycle_state='rejected')
 *   - user-role messages (not rendered from ChatMessage in that case)
 *
 * Optimistic UI:
 *   - Button turns solid/colored immediately on click (before network).
 *   - On 4xx/5xx: roll back to unsubmitted + show inline error alert for 4s.
 *   - On success: show "Thanks!" affordance for 2s, then disable both buttons.
 *   - Post-submit: both buttons disabled (one feedback per run; server allows
 *     multiple rows but UI enforces single submission UX).
 *
 * A11y:
 *   - Buttons: distinct aria-labels, 44×44px touch target via shadcn Button sm.
 *   - Popover: role="dialog", aria-label, aria-modal.
 *   - Category radios: <fieldset><legend> wrapping.
 */

import { useState, useRef, useCallback } from 'react';
import { ThumbsUp, ThumbsDown, X } from 'lucide-react';
import { Popover as RadixPopover, RadioGroup as RadixRadioGroup, Label as RadixLabel } from 'radix-ui';
import { Button } from '@/Components/ui/button';
import { cn } from '@/lib/utils';

// ── Constants ─────────────────────────────────────────────────────────────

const FASTAPI_BASE = '/fastapi';  // Laravel proxies /fastapi/* to FastAPI

const NOTE_MAX = 1000;

const CATEGORIES: { value: string; label: string; description: string }[] = [
    { value: 'hallucinated',   label: 'Hallucinated',   description: 'Model invented or fabricated facts' },
    { value: 'wrong_facts',    label: 'Wrong facts',    description: 'Factually incorrect but plausible' },
    { value: 'missing_info',   label: 'Missing info',   description: 'Incomplete or missing relevant context' },
    { value: 'off_topic',      label: 'Off topic',      description: 'Answer not relevant to the question' },
    { value: 'citation_issue', label: 'Citation issue', description: 'Wrong, missing, or mismatched citations' },
    { value: 'length_issue',   label: 'Length issue',   description: 'Answer too long or too short' },
];

// ── Sub-components ────────────────────────────────────────────────────────

interface InlineErrorProps {
    message: string;
    onDismiss: () => void;
}

function InlineError({ message, onDismiss }: InlineErrorProps) {
    return (
        <div
            role="alert"
            aria-live="assertive"
            className="flex items-start gap-2 mt-1.5 px-2.5 py-1.5 text-xs text-red-300 bg-red-950/40 border border-red-800/50 rounded"
        >
            <span className="flex-1">{message}</span>
            <button
                type="button"
                onClick={onDismiss}
                className="shrink-0 text-red-400 hover:text-red-200 focus:outline-none"
                aria-label="Dismiss error"
            >
                <X className="w-3 h-3" aria-hidden="true" />
            </button>
        </div>
    );
}

// ── Main component ────────────────────────────────────────────────────────

interface FeedbackButtonsProps {
    answerRunId: string | null | undefined;
    isStreaming?: boolean;
}

export type FeedbackState =
    | 'idle'
    | 'optimistic-up'
    | 'optimistic-down'
    | 'submitting'
    | 'submitted-up'
    | 'submitted-down'
    | 'error';

export function FeedbackButtons({ answerRunId, isStreaming }: FeedbackButtonsProps) {
    const [state, setState] = useState<FeedbackState>('idle');
    const [popoverOpen, setPopoverOpen] = useState(false);
    const [selectedCategory, setSelectedCategory] = useState<string>('');
    const [note, setNote] = useState('');
    const [errorMsg, setErrorMsg] = useState<string | null>(null);
    const [thanks, setThanks] = useState(false);

    const thumbsUpRef = useRef<HTMLButtonElement>(null);
    const thumbsDownRef = useRef<HTMLButtonElement>(null);

    const isSubmitted = state === 'submitted-up' || state === 'submitted-down';
    const isSubmitting = state === 'submitting';

    // Clear error after 4s
    const showError = useCallback((msg: string) => {
        setErrorMsg(msg);
        setTimeout(() => setErrorMsg(null), 4000);
    }, []);

    const showThanks = useCallback(() => {
        setThanks(true);
        setTimeout(() => setThanks(false), 2000);
    }, []);

    async function submitFeedback(polarity: 'up' | 'down', category?: string, noteText?: string) {
        if (!answerRunId) return;

        setState('submitting');

        const body: Record<string, unknown> = { polarity };
        if (category) body.category = category;
        if (noteText && noteText.trim()) body.note = noteText.trim().slice(0, 2000);

        try {
            // FASTAPI_BASE = '/fastapi' is a same-origin nginx proxy.
            // Auth via Sanctum session cookie. No bearer token from localStorage —
            // localStorage is an XSS-exfiltration target (types.ts:11-12).
            const csrfToken = document
                .querySelector('meta[name="csrf-token"]')
                ?.getAttribute('content');

            const resp = await fetch(`${FASTAPI_BASE}/v1/answer_runs/${answerRunId}/feedback`, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    ...(csrfToken ? { 'X-CSRF-TOKEN': csrfToken } : {}),
                },
                body: JSON.stringify(body),
            });

            if (!resp.ok) {
                let detail = `Submission failed (${resp.status})`;
                try {
                    const errBody = await resp.json();
                    if (errBody.detail) detail = String(errBody.detail);
                    else if (errBody.message) detail = String(errBody.message);
                } catch { /* ignore */ }
                throw new Error(detail);
            }

            // Success path
            setState(polarity === 'up' ? 'submitted-up' : 'submitted-down');
            showThanks();
        } catch (err: unknown) {
            // Rollback to idle so user can retry
            setState('idle');
            showError(err instanceof Error ? err.message : 'Failed to submit feedback. Please try again.');
        }
    }

    function handleThumbsUp() {
        if (isSubmitted || isSubmitting || isStreaming) return;
        setState('optimistic-up');
        submitFeedback('up');
    }

    function handleThumbsDownOpen() {
        if (isSubmitted || isSubmitting || isStreaming) return;
        setState('optimistic-down');
        setPopoverOpen(true);
    }

    function handlePopoverSubmit() {
        if (!selectedCategory) return;
        setPopoverOpen(false);
        submitFeedback('down', selectedCategory, note);
    }

    function handlePopoverCancel() {
        setPopoverOpen(false);
        // Rollback optimistic down if user cancels
        if (state === 'optimistic-down') setState('idle');
        setSelectedCategory('');
        setNote('');
    }

    const noteCharsLeft = NOTE_MAX - note.length;

    return (
        <div className="mt-2" data-testid="feedback-buttons">
            <div className="flex items-center gap-1.5">
                {/* Thumbs Up */}
                <Button
                    ref={thumbsUpRef}
                    type="button"
                    size="sm"
                    variant="ghost"
                    onClick={handleThumbsUp}
                    disabled={isSubmitted || isSubmitting || isStreaming}
                    aria-label="Mark answer as helpful"
                    aria-pressed={state === 'submitted-up' || state === 'optimistic-up'}
                    className={cn(
                        'text-xs gap-1.5 h-7 min-w-[44px] min-h-[44px] transition-colors',
                        (state === 'submitted-up' || state === 'optimistic-up')
                            ? 'text-green-400 bg-green-950/40 border border-green-700/50'
                            : 'text-gray-400 hover:text-green-400 hover:bg-green-950/30',
                        (isSubmitted || isSubmitting) && 'opacity-60 cursor-not-allowed',
                    )}
                >
                    <ThumbsUp className="h-3.5 w-3.5" aria-hidden="true" />
                    Helpful
                </Button>

                {/* Thumbs Down — opens Popover for category */}
                <RadixPopover.Root open={popoverOpen} onOpenChange={(open) => {
                    if (!open) handlePopoverCancel();
                    else setPopoverOpen(true);
                }}>
                    <RadixPopover.Trigger asChild>
                        <Button
                            ref={thumbsDownRef}
                            type="button"
                            size="sm"
                            variant="ghost"
                            onClick={handleThumbsDownOpen}
                            disabled={isSubmitted || isSubmitting || isStreaming}
                            aria-label="Report answer problem"
                            aria-pressed={state === 'submitted-down' || state === 'optimistic-down'}
                            aria-haspopup="dialog"
                            aria-expanded={popoverOpen}
                            className={cn(
                                'text-xs gap-1.5 h-7 min-w-[44px] min-h-[44px] transition-colors',
                                (state === 'submitted-down' || state === 'optimistic-down')
                                    ? 'text-red-400 bg-red-950/40 border border-red-700/50'
                                    : 'text-gray-400 hover:text-red-400 hover:bg-red-950/30',
                                (isSubmitted || isSubmitting) && 'opacity-60 cursor-not-allowed',
                            )}
                        >
                            <ThumbsDown className="h-3.5 w-3.5" aria-hidden="true" />
                            Report problem
                        </Button>
                    </RadixPopover.Trigger>

                    <RadixPopover.Portal>
                        <RadixPopover.Content
                            side="top"
                            align="start"
                            sideOffset={8}
                            role="dialog"
                            aria-modal="true"
                            aria-label="Report answer problem"
                            className={cn(
                                'z-50 w-80 rounded-lg border border-gray-700 bg-gray-900 p-4 shadow-xl',
                                'focus:outline-none',
                                'data-[state=open]:animate-in data-[state=closed]:animate-out',
                                'data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0',
                                'data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95',
                            )}
                            onEscapeKeyDown={handlePopoverCancel}
                            onInteractOutside={handlePopoverCancel}
                        >
                            <div className="flex items-start justify-between mb-3">
                                <h3 id="feedback-popover-title" className="text-sm font-medium text-gray-100">
                                    What was wrong with this answer?
                                </h3>
                                <button
                                    type="button"
                                    onClick={handlePopoverCancel}
                                    className="text-gray-500 hover:text-gray-200 focus:outline-none focus:ring-2 focus:ring-amber-500 rounded p-0.5"
                                    aria-label="Close feedback dialog"
                                >
                                    <X className="w-4 h-4" aria-hidden="true" />
                                </button>
                            </div>

                            {/* Category selection — fieldset/legend for a11y */}
                            <fieldset className="mb-3">
                                <legend className="text-xs text-gray-400 mb-2">
                                    Select a category <span className="text-red-400" aria-hidden="true">*</span>
                                    <span className="sr-only">(required)</span>
                                </legend>
                                <RadixRadioGroup.Root
                                    value={selectedCategory}
                                    onValueChange={setSelectedCategory}
                                    aria-required="true"
                                    className="space-y-1.5"
                                    data-testid="category-radio-group"
                                >
                                    {CATEGORIES.map((cat) => (
                                        <div key={cat.value} className="flex items-start gap-2">
                                            <RadixRadioGroup.Item
                                                value={cat.value}
                                                id={`feedback-cat-${cat.value}`}
                                                className={cn(
                                                    'mt-0.5 w-4 h-4 shrink-0 rounded-full border border-gray-600',
                                                    'focus:outline-none focus:ring-2 focus:ring-amber-500',
                                                    'data-[state=checked]:border-amber-500',
                                                    'data-[state=checked]:bg-amber-500',
                                                )}
                                            >
                                                <RadixRadioGroup.Indicator className="flex items-center justify-center w-full h-full relative after:content-[\'\'] after:block after:w-1.5 after:h-1.5 after:rounded-full after:bg-gray-900" />
                                            </RadixRadioGroup.Item>
                                            <RadixLabel.Root
                                                htmlFor={`feedback-cat-${cat.value}`}
                                                className="text-xs cursor-pointer"
                                            >
                                                <span className="text-gray-200 font-medium">{cat.label}</span>
                                                <span className="block text-gray-500">{cat.description}</span>
                                            </RadixLabel.Root>
                                        </div>
                                    ))}
                                </RadixRadioGroup.Root>
                            </fieldset>

                            {/* Optional comment */}
                            <div className="mb-3">
                                <label
                                    htmlFor="feedback-note"
                                    className="block text-xs text-gray-400 mb-1"
                                >
                                    Additional comments (optional)
                                </label>
                                <textarea
                                    id="feedback-note"
                                    value={note}
                                    onChange={(e) => setNote(e.target.value.slice(0, NOTE_MAX))}
                                    maxLength={NOTE_MAX}
                                    rows={3}
                                    placeholder="Describe the issue…"
                                    className={cn(
                                        'w-full resize-none rounded border border-gray-700 bg-gray-800',
                                        'px-2.5 py-1.5 text-xs text-gray-100 placeholder-gray-600',
                                        'focus:outline-none focus:ring-2 focus:ring-amber-500 focus:border-transparent',
                                    )}
                                    aria-describedby="feedback-note-count"
                                />
                                <span
                                    id="feedback-note-count"
                                    className={cn(
                                        'text-[10px] font-mono',
                                        noteCharsLeft < 50 ? 'text-amber-400' : 'text-gray-600',
                                    )}
                                    aria-live="polite"
                                >
                                    {noteCharsLeft} characters remaining
                                </span>
                            </div>

                            {/* Actions */}
                            <div className="flex gap-2 justify-end">
                                <Button
                                    type="button"
                                    size="sm"
                                    variant="ghost"
                                    onClick={handlePopoverCancel}
                                    className="text-xs h-7 text-gray-400"
                                >
                                    Cancel
                                </Button>
                                <Button
                                    type="button"
                                    size="sm"
                                    onClick={handlePopoverSubmit}
                                    disabled={!selectedCategory}
                                    className={cn(
                                        'text-xs h-7 bg-amber-600 hover:bg-amber-500 text-white',
                                        'disabled:opacity-50 disabled:cursor-not-allowed',
                                    )}
                                    aria-disabled={!selectedCategory}
                                >
                                    Submit feedback
                                </Button>
                            </div>

                            <RadixPopover.Arrow className="fill-gray-700" />
                        </RadixPopover.Content>
                    </RadixPopover.Portal>
                </RadixPopover.Root>

                {/* "Thanks!" affordance after successful submission */}
                {thanks && (
                    <span
                        className="text-xs text-green-400 ml-1"
                        role="status"
                        aria-live="polite"
                        aria-label="Feedback submitted successfully"
                    >
                        Thanks!
                    </span>
                )}
            </div>

            {/* Error rollback notice */}
            {errorMsg && (
                <InlineError
                    message={errorMsg}
                    onDismiss={() => setErrorMsg(null)}
                />
            )}
        </div>
    );
}
