import { useState } from 'react';
import type { JSX } from 'react';
import { Head, Link, router, usePage } from '@inertiajs/react';
import AppLayout from '../../../Layouts/AppLayout';

/**
 * /admin/agent-config/workspaces — Phase 0 Step 5.2.
 *
 * Lists workspace.workspace_agent_config (per-workspace agent overrides).
 * Operators toggle enabled/disabled and edit the JSONB config blob.
 * Saves are atomic with an audit_ledger entry under
 * workspace.workspace_agent_config.update.
 */

interface WorkspaceAgentConfig {
    id: string;
    workspace_id: string;
    agent_name: string;
    config: Record<string, unknown>;
    enabled: boolean;
    updated_at: string;
    updated_by: number | null;
}

interface PageProps {
    workspace_agent_configs: WorkspaceAgentConfig[];
    [key: string]: unknown;
}

interface FlashProps {
    flash?: { success?: string };
    errors?: Record<string, string>;
    [key: string]: unknown;
}

function shortId(id: string, head = 8): string {
    return id.length > head + 1 ? `${id.slice(0, head)}…` : id;
}

export default function Workspaces({ workspace_agent_configs }: PageProps): JSX.Element {
    const { props } = usePage<FlashProps>();
    const flash = props.flash?.success ?? null;

    return (
        <AppLayout>
            <Head title="Workspace agent config — Admin" />
            <div className="min-h-screen bg-stone-950 text-stone-100">
                <div className="mx-auto max-w-7xl px-6 py-8" data-testid="agent-config-workspaces">
                    <Link
                        href="/dashboard"
                        className="mb-4 inline-block text-sm text-stone-400 hover:text-amber-300"
                    >
                        ← Back to dashboard
                    </Link>

                    <header className="mb-6">
                        <h1 className="text-2xl font-semibold text-stone-50">Workspace agent config</h1>
                        <p className="mt-1 text-sm text-stone-400">
                            Per-workspace overrides from{' '}
                            <code className="text-stone-300">workspace.workspace_agent_config</code>. The wrapper merges
                            global default → this row → invocation context.
                        </p>
                    </header>

                    {flash && (
                        <div
                            className="mb-4 rounded border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300"
                            data-testid="flash-success"
                        >
                            {flash}
                        </div>
                    )}

                    {workspace_agent_configs.length === 0 ? (
                        <div className="rounded border border-stone-800 bg-stone-900 px-3 py-8 text-center text-stone-500">
                            No per-workspace overrides yet. The wrapper uses the global default for every
                            (workspace, agent) pair.
                        </div>
                    ) : (
                        <div className="space-y-4">
                            {workspace_agent_configs.map((row) => (
                                <ConfigCard key={row.id} row={row} />
                            ))}
                        </div>
                    )}
                </div>
            </div>
        </AppLayout>
    );
}

function ConfigCard({ row }: { row: WorkspaceAgentConfig }): JSX.Element {
    const [enabled, setEnabled] = useState(row.enabled);
    const [text, setText] = useState(JSON.stringify(row.config, null, 2));
    const [parseError, setParseError] = useState<string | null>(null);
    const [saving, setSaving] = useState(false);

    const onSave = (): void => {
        let parsed: unknown;
        try {
            parsed = JSON.parse(text);
        } catch (e) {
            setParseError(`Invalid JSON: ${(e as Error).message}`);
            return;
        }
        if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
            setParseError('config must be a JSON object (not a list or scalar).');
            return;
        }
        setParseError(null);
        setSaving(true);
        router.patch(
            `/admin/agent-config/workspaces/${row.id}`,
            { enabled, config: JSON.stringify(parsed) },
            {
                preserveScroll: true,
                onFinish: () => setSaving(false),
            },
        );
    };

    return (
        <section
            className="overflow-hidden rounded border border-stone-800 bg-stone-900"
            data-testid="workspace-config-card"
        >
            <header className="flex flex-wrap items-center gap-3 border-b border-stone-800 bg-stone-900/60 px-4 py-3">
                <span className="font-mono text-sm text-stone-100">{row.agent_name}</span>
                <span className="text-xs text-stone-500">workspace</span>
                <code className="font-mono text-xs text-stone-300" title={row.workspace_id}>
                    {shortId(row.workspace_id)}
                </code>
                <label className="ml-auto flex items-center gap-2 text-xs text-stone-300">
                    <input
                        type="checkbox"
                        checked={enabled}
                        onChange={(e) => setEnabled(e.target.checked)}
                        data-testid="enabled-toggle"
                    />
                    enabled
                </label>
            </header>
            <div className="p-4">
                <label className="mb-1 block text-xs uppercase tracking-wide text-stone-400">config (JSON)</label>
                <textarea
                    value={text}
                    onChange={(e) => setText(e.target.value)}
                    rows={8}
                    spellCheck={false}
                    className="w-full rounded border border-stone-700 bg-stone-800 p-2 font-mono text-xs text-stone-100 focus:border-amber-500 focus:outline-none"
                    data-testid="config-textarea"
                />
                {parseError && (
                    <p className="mt-2 text-xs text-red-300" data-testid="parse-error">
                        {parseError}
                    </p>
                )}
                <div className="mt-3 flex justify-end">
                    <button
                        type="button"
                        onClick={onSave}
                        disabled={saving}
                        className="rounded bg-amber-500 px-3 py-1 text-xs font-medium text-stone-950 hover:bg-amber-400 disabled:opacity-50"
                        data-testid="save-button"
                    >
                        {saving ? 'Saving…' : 'Save'}
                    </button>
                </div>
            </div>
        </section>
    );
}
