import type { JSX } from 'react';
import { useState } from 'react';
import { Head, Link, router, useForm } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';

/**
 * /admin/decisions/new — Master-plan §21.3 manual decision-entry surface
 * (doc-phase 158).
 *
 * Admin fills in a §21 decision of any of the 8 §21.3 types. Submission
 * POSTs to /admin/decisions which calls
 * App\Services\DecisionIntelligence\RecordDecision (doc-phase 133),
 * persisting:
 *   - silver.decision_records
 *   - silver.decision_evidence_links (per evidence chunk id)
 *   - silver.decision_options (per considered option)
 *   - audit.audit_ledger anchor + back-fill of audit_ledger_id + hash
 *
 * Backend: DecisionHistoryController::create / ::store.
 */

interface OptionEntry {
    label: string;
    description: string;
    was_chosen: boolean;
}

interface PageProps {
    valid_decision_types: string[];
    valid_human_decisions: string[];
    platform_ops_workspace_id: string;
}

export default function DecisionNew({
    valid_decision_types,
    valid_human_decisions,
    platform_ops_workspace_id,
}: PageProps): JSX.Element {
    const { data, setData, post, processing, errors, reset } = useForm({
        workspace_id: '',
        decision_type: valid_decision_types[0] ?? 'workflow_enablement',
        recommendation: '',
        human_decision: 'accepted',
        reason: '',
        uncertainty: '',
        evidence_chunk_ids: [] as string[],
        options_considered: [] as OptionEntry[],
    });

    const [chunkInput, setChunkInput] = useState('');

    function addChunk() {
        if (chunkInput.trim()) {
            setData('evidence_chunk_ids', [
                ...data.evidence_chunk_ids,
                chunkInput.trim(),
            ]);
            setChunkInput('');
        }
    }

    function removeChunk(idx: number) {
        setData(
            'evidence_chunk_ids',
            data.evidence_chunk_ids.filter((_: string, i: number) => i !== idx),
        );
    }

    function addOption() {
        setData('options_considered', [
            ...data.options_considered,
            { label: '', description: '', was_chosen: false },
        ]);
    }

    function updateOption(idx: number, key: keyof OptionEntry, value: string | boolean) {
        const next = [...data.options_considered];
        next[idx] = { ...next[idx], [key]: value };
        setData('options_considered', next);
    }

    function removeOption(idx: number) {
        setData(
            'options_considered',
            data.options_considered.filter((_: OptionEntry, i: number) => i !== idx),
        );
    }

    function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
        e.preventDefault();
        post('/admin/decisions', {
            preserveScroll: true,
            onSuccess: () => reset(),
        });
    }

    return (
        <AppLayout>
            <Head title="File a Decision — Admin" />
            <div className="min-h-screen bg-stone-950 text-stone-100">
                <div
                    className="mx-auto max-w-3xl px-6 py-8"
                    data-testid="decision-new"
                >
                    <Link
                        href="/admin/decision-history"
                        className="mb-4 inline-block text-sm text-stone-400 hover:text-amber-300"
                    >
                        ← Back to Decision History
                    </Link>

                    <header className="mb-6">
                        <h1 className="text-2xl font-semibold text-stone-50">
                            File a §21 Decision
                        </h1>
                        <p className="mt-1 text-sm text-stone-400">
                            Authentic capture of a §21.3 decision. Use this surface
                            when the parent flow lacks a built-in approval step
                            (crs_decision during OCR, schema_mapping during ingest,
                            etc.). Master-plan §9.12 / §21.
                        </p>
                    </header>

                    <form onSubmit={handleSubmit} className="space-y-5">
                        {/* decision_type */}
                        <div>
                            <label
                                htmlFor="decision_type"
                                className="block text-xs uppercase tracking-wide text-stone-500"
                            >
                                Decision type
                            </label>
                            <select
                                id="decision_type"
                                value={data.decision_type}
                                onChange={(e) => setData('decision_type', e.target.value)}
                                className="mt-1 block w-full rounded border border-stone-700 bg-stone-900 px-3 py-2 text-sm text-stone-100 focus:border-amber-500 focus:outline-none"
                            >
                                {valid_decision_types.map((dt) => (
                                    <option key={dt} value={dt}>
                                        {dt}
                                    </option>
                                ))}
                            </select>
                            {errors.decision_type && (
                                <p className="mt-1 text-xs text-red-300">{errors.decision_type}</p>
                            )}
                        </div>

                        {/* workspace_id */}
                        <div>
                            <label
                                htmlFor="workspace_id"
                                className="block text-xs uppercase tracking-wide text-stone-500"
                            >
                                Workspace ID (UUID, optional)
                            </label>
                            <input
                                id="workspace_id"
                                type="text"
                                value={data.workspace_id}
                                onChange={(e) => setData('workspace_id', e.target.value)}
                                placeholder={`Leave blank for platform_ops (${platform_ops_workspace_id.slice(0, 8)}…)`}
                                className="mt-1 block w-full rounded border border-stone-700 bg-stone-900 px-3 py-2 font-mono text-xs text-stone-100 focus:border-amber-500 focus:outline-none"
                            />
                            {errors.workspace_id && (
                                <p className="mt-1 text-xs text-red-300">{errors.workspace_id}</p>
                            )}
                        </div>

                        {/* recommendation */}
                        <div>
                            <label
                                htmlFor="recommendation"
                                className="block text-xs uppercase tracking-wide text-stone-500"
                            >
                                AI / system recommendation
                            </label>
                            <textarea
                                id="recommendation"
                                rows={3}
                                value={data.recommendation}
                                onChange={(e) => setData('recommendation', e.target.value)}
                                placeholder="What the system suggested (e.g. 'Map column au_g_t to silver.assays.au_ppm')"
                                className="mt-1 block w-full rounded border border-stone-700 bg-stone-900 px-3 py-2 text-sm text-stone-100 focus:border-amber-500 focus:outline-none"
                                required
                            />
                            {errors.recommendation && (
                                <p className="mt-1 text-xs text-red-300">{errors.recommendation}</p>
                            )}
                        </div>

                        {/* human_decision */}
                        <div>
                            <label
                                htmlFor="human_decision"
                                className="block text-xs uppercase tracking-wide text-stone-500"
                            >
                                Human decision
                            </label>
                            <select
                                id="human_decision"
                                value={data.human_decision}
                                onChange={(e) => setData('human_decision', e.target.value)}
                                className="mt-1 block w-full rounded border border-stone-700 bg-stone-900 px-3 py-2 text-sm text-stone-100 focus:border-amber-500 focus:outline-none"
                            >
                                {valid_human_decisions.map((hd) => (
                                    <option key={hd} value={hd}>
                                        {hd}
                                    </option>
                                ))}
                            </select>
                            {errors.human_decision && (
                                <p className="mt-1 text-xs text-red-300">{errors.human_decision}</p>
                            )}
                        </div>

                        {/* reason */}
                        <div>
                            <label
                                htmlFor="reason"
                                className="block text-xs uppercase tracking-wide text-stone-500"
                            >
                                Reason (optional)
                            </label>
                            <textarea
                                id="reason"
                                rows={3}
                                value={data.reason}
                                onChange={(e) => setData('reason', e.target.value)}
                                placeholder="Why this human_decision was reached"
                                className="mt-1 block w-full rounded border border-stone-700 bg-stone-900 px-3 py-2 text-sm text-stone-100 focus:border-amber-500 focus:outline-none"
                            />
                            {errors.reason && (
                                <p className="mt-1 text-xs text-red-300">{errors.reason}</p>
                            )}
                        </div>

                        {/* uncertainty */}
                        <div>
                            <label
                                htmlFor="uncertainty"
                                className="block text-xs uppercase tracking-wide text-stone-500"
                            >
                                Uncertainty (optional, 0..1)
                            </label>
                            <input
                                id="uncertainty"
                                type="number"
                                min="0"
                                max="1"
                                step="0.01"
                                value={data.uncertainty}
                                onChange={(e) => setData('uncertainty', e.target.value)}
                                placeholder="0.15"
                                className="mt-1 block w-32 rounded border border-stone-700 bg-stone-900 px-3 py-2 font-mono text-sm text-stone-100 focus:border-amber-500 focus:outline-none"
                            />
                            {errors.uncertainty && (
                                <p className="mt-1 text-xs text-red-300">{errors.uncertainty}</p>
                            )}
                        </div>

                        {/* evidence_chunk_ids */}
                        <div>
                            <label className="block text-xs uppercase tracking-wide text-stone-500">
                                Evidence chunk IDs (optional)
                            </label>
                            <div className="mt-1 flex gap-2">
                                <input
                                    type="text"
                                    value={chunkInput}
                                    onChange={(e) => setChunkInput(e.target.value)}
                                    placeholder="chunk_xxx"
                                    onKeyDown={(e) => {
                                        if (e.key === 'Enter') {
                                            e.preventDefault();
                                            addChunk();
                                        }
                                    }}
                                    className="flex-1 rounded border border-stone-700 bg-stone-900 px-3 py-2 font-mono text-xs text-stone-100 focus:border-amber-500 focus:outline-none"
                                />
                                <button
                                    type="button"
                                    onClick={addChunk}
                                    className="rounded border border-stone-700 bg-stone-800 px-3 py-2 text-xs text-stone-300 hover:border-amber-500 hover:text-amber-300"
                                >
                                    + Add
                                </button>
                            </div>
                            {data.evidence_chunk_ids.length > 0 && (
                                <ul className="mt-2 flex flex-wrap gap-1">
                                    {data.evidence_chunk_ids.map((c: string, idx: number) => (
                                        <li
                                            key={idx}
                                            className="flex items-center gap-1 rounded border border-stone-700 bg-stone-800/40 px-2 py-0.5 font-mono text-xs text-stone-300"
                                        >
                                            {c}
                                            <button
                                                type="button"
                                                onClick={() => removeChunk(idx)}
                                                className="text-stone-500 hover:text-red-400"
                                                aria-label={`Remove ${c}`}
                                            >
                                                ✕
                                            </button>
                                        </li>
                                    ))}
                                </ul>
                            )}
                        </div>

                        {/* options_considered */}
                        <div>
                            <div className="flex items-center justify-between">
                                <label className="block text-xs uppercase tracking-wide text-stone-500">
                                    Options considered (optional)
                                </label>
                                <button
                                    type="button"
                                    onClick={addOption}
                                    className="rounded border border-stone-700 bg-stone-800 px-2 py-0.5 text-xs text-stone-300 hover:border-amber-500 hover:text-amber-300"
                                >
                                    + Add option
                                </button>
                            </div>
                            <div className="mt-2 space-y-2">
                                {data.options_considered.map((opt: OptionEntry, idx: number) => (
                                    <div
                                        key={idx}
                                        className="rounded border border-stone-800 bg-stone-900/50 p-3"
                                    >
                                        <div className="flex items-center gap-2">
                                            <input
                                                type="text"
                                                value={opt.label}
                                                onChange={(e) => updateOption(idx, 'label', e.target.value)}
                                                placeholder="label"
                                                className="flex-1 rounded border border-stone-700 bg-stone-900 px-2 py-1 text-xs text-stone-100 focus:border-amber-500 focus:outline-none"
                                            />
                                            <label className="flex items-center gap-1 text-xs text-stone-300">
                                                <input
                                                    type="checkbox"
                                                    checked={opt.was_chosen}
                                                    onChange={(e) => updateOption(idx, 'was_chosen', e.target.checked)}
                                                />
                                                chosen
                                            </label>
                                            <button
                                                type="button"
                                                onClick={() => removeOption(idx)}
                                                className="text-stone-500 hover:text-red-400"
                                                aria-label="Remove option"
                                            >
                                                ✕
                                            </button>
                                        </div>
                                        <input
                                            type="text"
                                            value={opt.description}
                                            onChange={(e) => updateOption(idx, 'description', e.target.value)}
                                            placeholder="description"
                                            className="mt-1 block w-full rounded border border-stone-700 bg-stone-900 px-2 py-1 text-xs text-stone-100 focus:border-amber-500 focus:outline-none"
                                        />
                                    </div>
                                ))}
                            </div>
                        </div>

                        <div className="flex items-center gap-3 pt-2">
                            <button
                                type="submit"
                                disabled={processing}
                                className="rounded border border-amber-500/60 bg-amber-500/20 px-4 py-2 text-sm font-medium text-amber-200 hover:border-amber-400 hover:bg-amber-500/30 disabled:opacity-50"
                            >
                                {processing ? 'Filing…' : 'File decision'}
                            </button>
                            <Link
                                href="/admin/decision-history"
                                className="text-sm text-stone-400 hover:text-stone-200"
                            >
                                Cancel
                            </Link>
                        </div>
                    </form>
                </div>
            </div>
        </AppLayout>
    );
}
