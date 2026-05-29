import type { JSX } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';

/**
 * /admin/eval/questions — Master-plan §10-v2 (doc-phase 179)
 * Golden Question Authoring UI — index page.
 *
 * Paginated, filterable table of every row in eval.golden_questions.
 * Drill into a row → EvalQuestionEditor (hybrid form + Monaco JSON).
 *
 * Backend: app/Http/Controllers/Admin/EvalQuestionsController::index.
 * FastAPI: GET /api/v1/admin/eval/questions
 */

interface GoldenQuestion {
    question_id: string;
    question_set: string;
    question_text: string;
    difficulty: string;
    status: string;
    authored_by_user_id: number;
    authored_at: string;
    reviewed_by_user_id: number | null;
    expected_refusal: boolean;
}

interface PageProps {
    questions: GoldenQuestion[];
    total: number;
    filters: {
        question_set?: string;
        status?: string;
        search?: string;
        limit?: number;
        offset?: number;
    };
    valid_sets: string[];
    valid_difficulties: string[];
    valid_statuses: string[];
}

function statusBadge(status: string): JSX.Element {
    const tone =
        status === 'active' ? 'bg-emerald-500/10 text-emerald-700 ring-emerald-500/20'
            : status === 'draft' ? 'bg-amber-500/10 text-amber-700 ring-amber-500/20'
                : 'bg-zinc-500/10 text-zinc-700 ring-zinc-500/20';
    return (
        <span className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${tone}`}>
            {status}
        </span>
    );
}

function difficultyChip(d: string): JSX.Element {
    const tone =
        d === 'hard' ? 'bg-red-500/10 text-red-700'
            : d === 'medium' ? 'bg-yellow-500/10 text-yellow-700'
                : 'bg-blue-500/10 text-blue-700';
    return <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ${tone}`}>{d}</span>;
}

function truncate(text: string, max = 120): string {
    return text.length <= max ? text : text.slice(0, max - 1) + '…';
}

export default function EvalQuestions({
    questions, total, filters, valid_sets, valid_statuses,
}: PageProps): JSX.Element {
    const limit = filters.limit ?? 50;
    const offset = filters.offset ?? 0;
    const showingFrom = total === 0 ? 0 : offset + 1;
    const showingTo = Math.min(offset + limit, total);

    function applyFilter(updates: Record<string, string | number | undefined>) {
        const next = { ...filters, ...updates, offset: 0 };
        Object.keys(next).forEach((k) => {
            const v = (next as Record<string, unknown>)[k];
            if (v === '' || v === undefined || v === null) {
                delete (next as Record<string, unknown>)[k];
            }
        });
        router.get(route('admin.eval.questions.index'), next, {
            preserveState: true, preserveScroll: true,
        });
    }

    function paginate(delta: number) {
        const newOffset = Math.max(0, offset + delta * limit);
        router.get(route('admin.eval.questions.index'),
            { ...filters, offset: newOffset, limit },
            { preserveState: true, preserveScroll: true },
        );
    }

    return (
        <AppLayout>
            <Head title="Golden Questions" />

            <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
                <div className="flex items-center justify-between">
                    <div>
                        <h1 className="text-2xl font-semibold text-zinc-900">
                            Golden Questions
                        </h1>
                        <p className="mt-1 text-sm text-zinc-500">
                            §10 eval harness · {total.toLocaleString()} total
                        </p>
                    </div>
                    <Link
                        href={route('admin.eval.questions.create')}
                        className="rounded-md bg-indigo-600 px-3 py-2 text-sm font-medium text-white shadow-sm hover:bg-indigo-500"
                    >
                        New question
                    </Link>
                </div>

                {/* Filters */}
                <div className="mt-6 grid grid-cols-1 gap-3 rounded-lg border border-zinc-200 bg-white p-4 sm:grid-cols-4">
                    <div>
                        <label className="block text-xs font-medium text-zinc-600">Question set</label>
                        <select
                            className="mt-1 block w-full rounded-md border-zinc-300 text-sm"
                            value={filters.question_set ?? ''}
                            onChange={(e) => applyFilter({ question_set: e.target.value || undefined })}
                        >
                            <option value="">(all)</option>
                            {valid_sets.map((s) => (
                                <option key={s} value={s}>{s}</option>
                            ))}
                        </select>
                    </div>
                    <div>
                        <label className="block text-xs font-medium text-zinc-600">Status</label>
                        <select
                            className="mt-1 block w-full rounded-md border-zinc-300 text-sm"
                            value={filters.status ?? ''}
                            onChange={(e) => applyFilter({ status: e.target.value || undefined })}
                        >
                            <option value="">(all)</option>
                            {valid_statuses.map((s) => (
                                <option key={s} value={s}>{s}</option>
                            ))}
                        </select>
                    </div>
                    <div className="sm:col-span-2">
                        <label className="block text-xs font-medium text-zinc-600">Search question text</label>
                        <input
                            type="text"
                            className="mt-1 block w-full rounded-md border-zinc-300 text-sm"
                            defaultValue={filters.search ?? ''}
                            placeholder="ILIKE match on question_text"
                            onKeyDown={(e) => {
                                if (e.key === 'Enter') {
                                    applyFilter({ search: (e.target as HTMLInputElement).value || undefined });
                                }
                            }}
                        />
                    </div>
                </div>

                {/* Table */}
                <div className="mt-4 overflow-hidden rounded-lg border border-zinc-200 bg-white">
                    <table className="min-w-full divide-y divide-zinc-200 text-sm">
                        <thead className="bg-zinc-50">
                            <tr>
                                <th className="px-3 py-2 text-left font-medium text-zinc-700">Question</th>
                                <th className="px-3 py-2 text-left font-medium text-zinc-700">Set</th>
                                <th className="px-3 py-2 text-left font-medium text-zinc-700">Difficulty</th>
                                <th className="px-3 py-2 text-left font-medium text-zinc-700">Status</th>
                                <th className="px-3 py-2 text-left font-medium text-zinc-700">Refusal?</th>
                                <th className="px-3 py-2 text-left font-medium text-zinc-700">Authored</th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-zinc-100">
                            {questions.length === 0 && (
                                <tr>
                                    <td colSpan={6} className="px-3 py-6 text-center text-zinc-500">
                                        No questions match the current filters.
                                    </td>
                                </tr>
                            )}
                            {questions.map((q) => (
                                <tr key={q.question_id} className="hover:bg-zinc-50">
                                    <td className="px-3 py-2">
                                        <Link
                                            href={route('admin.eval.questions.show', { id: q.question_id })}
                                            className="text-indigo-600 hover:underline"
                                        >
                                            {truncate(q.question_text, 100)}
                                        </Link>
                                    </td>
                                    <td className="px-3 py-2 font-mono text-xs text-zinc-600">{q.question_set}</td>
                                    <td className="px-3 py-2">{difficultyChip(q.difficulty)}</td>
                                    <td className="px-3 py-2">{statusBadge(q.status)}</td>
                                    <td className="px-3 py-2 text-zinc-600">
                                        {q.expected_refusal ? 'yes' : '—'}
                                    </td>
                                    <td className="px-3 py-2 text-xs text-zinc-500">
                                        {new Date(q.authored_at).toISOString().slice(0, 10)}
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>

                {/* Pagination */}
                <div className="mt-3 flex items-center justify-between text-sm text-zinc-600">
                    <div>
                        Showing {showingFrom}–{showingTo} of {total.toLocaleString()}
                    </div>
                    <div className="flex gap-2">
                        <button
                            type="button"
                            onClick={() => paginate(-1)}
                            disabled={offset === 0}
                            className="rounded border border-zinc-300 px-3 py-1 disabled:opacity-50"
                        >
                            ← Prev
                        </button>
                        <button
                            type="button"
                            onClick={() => paginate(1)}
                            disabled={offset + limit >= total}
                            className="rounded border border-zinc-300 px-3 py-1 disabled:opacity-50"
                        >
                            Next →
                        </button>
                    </div>
                </div>
            </div>
        </AppLayout>
    );
}
