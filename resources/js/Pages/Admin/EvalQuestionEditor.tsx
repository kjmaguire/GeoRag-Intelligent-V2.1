import type { JSX } from 'react';
import { useMemo, useState } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';

/**
 * /admin/eval/questions/{id} (and /new) — Master-plan §10-v2 (doc-phase 179)
 * Golden Question Editor — hybrid form + JSON textareas.
 *
 * Top-level scalars (set, difficulty, status, refusal, text) are
 * normal form controls; the five jsonb columns are JSON textareas
 * with client-side parse-on-submit. We can swap the textareas for
 * Monaco later by dropping in @monaco-editor/react — none of the
 * surrounding form shape changes.
 *
 * Status transitions are admin-only buttons in the sidebar.
 * The "Dry-run" button hits FastAPI's synthetic evaluator and shows
 * the result inline (no row written).
 */

interface GoldenQuestion {
    question_id: string;
    question_set: string;
    question_text: string;
    context_setup: Record<string, unknown>;
    expected_intent_class: string | null;
    expected_citations: unknown[];
    expected_entities: unknown[];
    expected_numeric_values: unknown[];
    expected_refusal: boolean;
    expected_refusal_reason: string | null;
    expected_language_compliance: unknown[];
    difficulty: string;
    status: string;
    authored_by_user_id: number;
    authored_at: string;
    reviewed_by_user_id: number | null;
    reviewed_at: string | null;
}

interface PageProps {
    question: GoldenQuestion | null;  // null = new question
    valid_sets: string[];
    valid_difficulties: string[];
    valid_statuses: string[];
}

interface DryRunResult {
    passed: boolean;
    failure_layer: string | null;
    failure_detail: string | null;
    latency_ms: number | null;
    actual_payload: Record<string, unknown>;
}

function blankQuestion(): GoldenQuestion {
    return {
        question_id: '',
        question_set: 'core_chat',
        question_text: '',
        context_setup: {},
        expected_intent_class: null,
        expected_citations: [],
        expected_entities: [],
        expected_numeric_values: [],
        expected_refusal: false,
        expected_refusal_reason: null,
        expected_language_compliance: [],
        difficulty: 'medium',
        status: 'draft',
        authored_by_user_id: 0,
        authored_at: '',
        reviewed_by_user_id: null,
        reviewed_at: null,
    };
}

function JsonField({
    label, value, onChange, rows = 6, error,
}: {
    label: string;
    value: string;
    onChange: (v: string) => void;
    rows?: number;
    error?: string | null;
}): JSX.Element {
    return (
        <div>
            <div className="flex items-center justify-between">
                <label className="block text-xs font-medium text-zinc-600">{label}</label>
                {error && <span className="text-xs text-red-600">{error}</span>}
            </div>
            <textarea
                className={`mt-1 block w-full rounded-md font-mono text-xs ${error ? 'border-red-300' : 'border-zinc-300'}`}
                rows={rows}
                value={value}
                onChange={(e) => onChange(e.target.value)}
                spellCheck={false}
            />
        </div>
    );
}

export default function EvalQuestionEditor({
    question, valid_sets, valid_difficulties, valid_statuses,
}: PageProps): JSX.Element {
    const isNew = question === null;
    const initial = question ?? blankQuestion();

    const [form, setForm] = useState(initial);
    const [jsonStrings, setJsonStrings] = useState({
        context_setup: JSON.stringify(initial.context_setup, null, 2),
        expected_citations: JSON.stringify(initial.expected_citations, null, 2),
        expected_entities: JSON.stringify(initial.expected_entities, null, 2),
        expected_numeric_values: JSON.stringify(initial.expected_numeric_values, null, 2),
        expected_language_compliance: JSON.stringify(initial.expected_language_compliance, null, 2),
    });
    const [submitting, setSubmitting] = useState(false);
    const [dryRun, setDryRun] = useState<DryRunResult | null>(null);

    const jsonErrors = useMemo(() => {
        const errors: Record<string, string | null> = {};
        for (const [k, v] of Object.entries(jsonStrings)) {
            try {
                JSON.parse(v);
                errors[k] = null;
            } catch (e) {
                errors[k] = (e as Error).message;
            }
        }
        return errors;
    }, [jsonStrings]);

    const hasJsonErrors = useMemo(
        () => Object.values(jsonErrors).some((e) => e !== null),
        [jsonErrors],
    );

    function submit(e: React.FormEvent) {
        e.preventDefault();
        if (hasJsonErrors) return;
        setSubmitting(true);

        const payload = {
            question_set: form.question_set,
            question_text: form.question_text,
            expected_intent_class: form.expected_intent_class,
            expected_refusal: form.expected_refusal,
            expected_refusal_reason: form.expected_refusal_reason,
            difficulty: form.difficulty,
            context_setup: JSON.parse(jsonStrings.context_setup),
            expected_citations: JSON.parse(jsonStrings.expected_citations),
            expected_entities: JSON.parse(jsonStrings.expected_entities),
            expected_numeric_values: JSON.parse(jsonStrings.expected_numeric_values),
            expected_language_compliance: JSON.parse(jsonStrings.expected_language_compliance),
        };

        const onFinish = () => setSubmitting(false);
        if (isNew) {
            router.post(route('admin.eval.questions.store'), payload, { onFinish });
        } else {
            router.put(route('admin.eval.questions.update', { id: form.question_id }), payload, { onFinish });
        }
    }

    function transition(status: string) {
        if (isNew) return;
        if (!confirm(`Transition this question to "${status}"?`)) return;
        fetch(route('admin.eval.questions.transition', { id: form.question_id }), {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-TOKEN': (document.querySelector('meta[name="csrf-token"]') as HTMLMetaElement)?.content ?? '',
                Accept: 'application/json',
            },
            body: JSON.stringify({ status }),
        })
            .then(async (r) => {
                if (!r.ok) throw new Error(await r.text());
                router.reload();
            })
            .catch((err) => alert(`Transition failed: ${err.message}`));
    }

    function runDryRun() {
        if (isNew) return;
        setDryRun(null);
        fetch(route('admin.eval.questions.dry-run', { id: form.question_id }), {
            method: 'POST',
            headers: {
                'X-CSRF-TOKEN': (document.querySelector('meta[name="csrf-token"]') as HTMLMetaElement)?.content ?? '',
                Accept: 'application/json',
            },
        })
            .then(async (r) => {
                if (!r.ok) throw new Error(await r.text());
                setDryRun(await r.json());
            })
            .catch((err) => alert(`Dry-run failed: ${err.message}`));
    }

    return (
        <AppLayout>
            <Head title={isNew ? 'New Question' : 'Edit Question'} />

            <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
                <div className="flex items-center justify-between">
                    <div>
                        <Link
                            href={route('admin.eval.questions.index')}
                            className="text-sm text-indigo-600 hover:underline"
                        >
                            ← All questions
                        </Link>
                        <h1 className="mt-1 text-2xl font-semibold text-zinc-900">
                            {isNew ? 'New Golden Question' : 'Edit Golden Question'}
                        </h1>
                        {!isNew && (
                            <p className="mt-1 text-sm text-zinc-500">
                                ID <span className="font-mono">{form.question_id}</span> ·
                                status <span className="font-medium">{form.status}</span>
                            </p>
                        )}
                    </div>
                </div>

                <form onSubmit={submit} className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-3">
                    {/* Left: structured form */}
                    <div className="rounded-lg border border-zinc-200 bg-white p-4 lg:col-span-1">
                        <h2 className="text-sm font-medium text-zinc-700">Structured fields</h2>

                        <div className="mt-4 space-y-4">
                            <div>
                                <label className="block text-xs font-medium text-zinc-600">Question set</label>
                                <select
                                    className="mt-1 block w-full rounded-md border-zinc-300 text-sm"
                                    value={form.question_set}
                                    onChange={(e) => setForm({ ...form, question_set: e.target.value })}
                                >
                                    {valid_sets.map((s) => (
                                        <option key={s} value={s}>{s}</option>
                                    ))}
                                </select>
                            </div>

                            <div>
                                <label className="block text-xs font-medium text-zinc-600">Question text</label>
                                <textarea
                                    rows={4}
                                    className="mt-1 block w-full rounded-md border-zinc-300 text-sm"
                                    value={form.question_text}
                                    onChange={(e) => setForm({ ...form, question_text: e.target.value })}
                                    required
                                    minLength={5}
                                    maxLength={2000}
                                />
                            </div>

                            <div>
                                <label className="block text-xs font-medium text-zinc-600">Difficulty</label>
                                <select
                                    className="mt-1 block w-full rounded-md border-zinc-300 text-sm"
                                    value={form.difficulty}
                                    onChange={(e) => setForm({ ...form, difficulty: e.target.value })}
                                >
                                    {valid_difficulties.map((d) => (
                                        <option key={d} value={d}>{d}</option>
                                    ))}
                                </select>
                            </div>

                            <div>
                                <label className="block text-xs font-medium text-zinc-600">Intent class</label>
                                <input
                                    type="text"
                                    className="mt-1 block w-full rounded-md border-zinc-300 text-sm"
                                    value={form.expected_intent_class ?? ''}
                                    onChange={(e) => setForm({ ...form, expected_intent_class: e.target.value || null })}
                                    maxLength={60}
                                    placeholder="(optional)"
                                />
                            </div>

                            <div>
                                <label className="flex items-center gap-2 text-xs font-medium text-zinc-600">
                                    <input
                                        type="checkbox"
                                        checked={form.expected_refusal}
                                        onChange={(e) => setForm({ ...form, expected_refusal: e.target.checked })}
                                    />
                                    Expected refusal
                                </label>
                            </div>

                            {form.expected_refusal && (
                                <div>
                                    <label className="block text-xs font-medium text-zinc-600">Refusal reason</label>
                                    <textarea
                                        rows={3}
                                        className="mt-1 block w-full rounded-md border-zinc-300 text-sm"
                                        value={form.expected_refusal_reason ?? ''}
                                        onChange={(e) => setForm({ ...form, expected_refusal_reason: e.target.value || null })}
                                    />
                                </div>
                            )}
                        </div>

                        {/* Transition + dry-run sidebar */}
                        {!isNew && (
                            <div className="mt-6 space-y-3 border-t border-zinc-200 pt-4">
                                <h3 className="text-sm font-medium text-zinc-700">Lifecycle</h3>
                                <div className="flex flex-wrap gap-2">
                                    {form.status === 'draft' && (
                                        <button
                                            type="button"
                                            onClick={() => transition('active')}
                                            className="rounded bg-emerald-600 px-3 py-1 text-xs font-medium text-white hover:bg-emerald-500"
                                        >
                                            Activate
                                        </button>
                                    )}
                                    {form.status === 'active' && (
                                        <button
                                            type="button"
                                            onClick={() => transition('retired')}
                                            className="rounded bg-zinc-700 px-3 py-1 text-xs font-medium text-white hover:bg-zinc-600"
                                        >
                                            Retire
                                        </button>
                                    )}
                                    {form.status === 'retired' && (
                                        <button
                                            type="button"
                                            onClick={() => transition('draft')}
                                            className="rounded bg-amber-600 px-3 py-1 text-xs font-medium text-white hover:bg-amber-500"
                                        >
                                            Un-retire
                                        </button>
                                    )}
                                </div>

                                <h3 className="text-sm font-medium text-zinc-700 pt-2">Dry-run</h3>
                                <button
                                    type="button"
                                    onClick={runDryRun}
                                    className="rounded border border-zinc-300 px-3 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50"
                                >
                                    Run synthetic evaluator
                                </button>
                                {dryRun && (
                                    <div className="mt-2 rounded border border-zinc-200 bg-zinc-50 p-2 text-xs">
                                        <div className="flex justify-between">
                                            <span className="font-medium">{dryRun.passed ? '✓ PASS' : '✗ FAIL'}</span>
                                            <span className="text-zinc-500">{dryRun.latency_ms ?? 0}ms</span>
                                        </div>
                                        {dryRun.failure_layer && (
                                            <div className="text-red-700">{dryRun.failure_layer}: {dryRun.failure_detail}</div>
                                        )}
                                        <pre className="mt-1 overflow-auto whitespace-pre-wrap text-[10px] text-zinc-600">
                                            {JSON.stringify(dryRun.actual_payload, null, 2)}
                                        </pre>
                                    </div>
                                )}
                            </div>
                        )}
                    </div>

                    {/* Right: JSON fields */}
                    <div className="space-y-4 rounded-lg border border-zinc-200 bg-white p-4 lg:col-span-2">
                        <h2 className="text-sm font-medium text-zinc-700">JSON fields (jsonb columns)</h2>

                        <JsonField
                            label="context_setup"
                            value={jsonStrings.context_setup}
                            onChange={(v) => setJsonStrings({ ...jsonStrings, context_setup: v })}
                            error={jsonErrors.context_setup}
                            rows={5}
                        />
                        <JsonField
                            label="expected_citations"
                            value={jsonStrings.expected_citations}
                            onChange={(v) => setJsonStrings({ ...jsonStrings, expected_citations: v })}
                            error={jsonErrors.expected_citations}
                            rows={5}
                        />
                        <JsonField
                            label="expected_entities"
                            value={jsonStrings.expected_entities}
                            onChange={(v) => setJsonStrings({ ...jsonStrings, expected_entities: v })}
                            error={jsonErrors.expected_entities}
                            rows={4}
                        />
                        <JsonField
                            label="expected_numeric_values"
                            value={jsonStrings.expected_numeric_values}
                            onChange={(v) => setJsonStrings({ ...jsonStrings, expected_numeric_values: v })}
                            error={jsonErrors.expected_numeric_values}
                            rows={4}
                        />
                        <JsonField
                            label="expected_language_compliance"
                            value={jsonStrings.expected_language_compliance}
                            onChange={(v) => setJsonStrings({ ...jsonStrings, expected_language_compliance: v })}
                            error={jsonErrors.expected_language_compliance}
                            rows={4}
                        />
                    </div>

                    {/* Footer actions */}
                    <div className="flex items-center justify-end gap-3 lg:col-span-3">
                        <Link
                            href={route('admin.eval.questions.index')}
                            className="rounded border border-zinc-300 px-4 py-2 text-sm font-medium text-zinc-700 hover:bg-zinc-50"
                        >
                            Cancel
                        </Link>
                        <button
                            type="submit"
                            disabled={hasJsonErrors || submitting}
                            className="rounded bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
                        >
                            {submitting ? 'Saving…' : (isNew ? 'Create draft' : 'Save changes')}
                        </button>
                    </div>
                </form>
            </div>
        </AppLayout>
    );
}
