import { useState } from 'react';
import type { JSX } from 'react';
import { Head, Link, router, usePage } from '@inertiajs/react';
import AppLayout from '../../../Layouts/AppLayout';

/**
 * /admin/agent-config/prompts — Phase 0 Step 5.2.
 *
 * Lists workspace.prompt_versions grouped by prompt_id, showing the
 * promotion lifecycle (draft / staging / production / deprecated).
 * Promotion writes the row + an audit_ledger entry inside one
 * DB transaction.
 */

const STATES = ['draft', 'staging', 'production', 'deprecated'] as const;
type State = (typeof STATES)[number];

interface PromptVersion {
    id: string;
    prompt_id: string;
    version: string;
    promotion_state: State;
    promoted_at: string | null;
    deprecated_at: string | null;
    created_at: string;
    created_by: number | null;
    notes: string | null;
}

interface PromptGroup {
    prompt_id: string;
    versions: PromptVersion[];
}

interface PageProps {
    prompts: PromptGroup[];
    [key: string]: unknown;
}

interface FlashProps {
    flash?: { success?: string };
    [key: string]: unknown;
}

const STATE_BADGE: Record<State, string> = {
    draft: 'bg-stone-700/40 text-stone-300 border-stone-600',
    staging: 'bg-sky-500/15 text-sky-300 border-sky-500/40',
    production: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40',
    deprecated: 'bg-stone-600/30 text-stone-500 border-stone-700',
};

export default function Prompts({ prompts }: PageProps): JSX.Element {
    const { props } = usePage<FlashProps>();
    const flash = props.flash?.success ?? null;

    return (
        <AppLayout>
            <Head title="Prompt versions — Admin" />
            <div className="min-h-screen bg-stone-950 text-stone-100">
                <div className="mx-auto max-w-7xl px-6 py-8" data-testid="agent-config-prompts">
                    <Link
                        href="/dashboard"
                        className="mb-4 inline-block text-sm text-stone-400 hover:text-amber-300"
                    >
                        ← Back to dashboard
                    </Link>

                    <header className="mb-6">
                        <h1 className="text-2xl font-semibold text-stone-50">Prompt versions</h1>
                        <p className="mt-1 text-sm text-stone-400">
                            Per-prompt version history from{' '}
                            <code className="text-stone-300">workspace.prompt_versions</code>. The Phase 4 Prompt Release
                            Approval Agent will eventually gate staging→production; until then admins promote by hand.
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

                    {prompts.length === 0 && (
                        <div className="rounded border border-stone-800 bg-stone-900 px-3 py-8 text-center text-stone-500">
                            No prompt versions yet.
                        </div>
                    )}

                    <div className="space-y-6">
                        {prompts.map((group) => (
                            <PromptCard key={group.prompt_id} group={group} />
                        ))}
                    </div>
                </div>
            </div>
        </AppLayout>
    );
}

function PromptCard({ group }: { group: PromptGroup }): JSX.Element {
    return (
        <section
            className="overflow-hidden rounded border border-stone-800 bg-stone-900"
            data-testid="prompt-group"
        >
            <header className="border-b border-stone-800 bg-stone-900/60 px-4 py-3">
                <h2 className="font-mono text-sm text-stone-100">{group.prompt_id}</h2>
            </header>
            <table className="w-full text-left text-sm">
                <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                    <tr>
                        <th className="px-3 py-2">Version</th>
                        <th className="px-3 py-2">State</th>
                        <th className="px-3 py-2">Promoted at</th>
                        <th className="px-3 py-2">Created at</th>
                        <th className="px-3 py-2">Notes</th>
                        <th className="px-3 py-2 text-right">Promote</th>
                    </tr>
                </thead>
                <tbody>
                    {group.versions.map((v) => (
                        <VersionRow key={v.id} version={v} />
                    ))}
                </tbody>
            </table>
        </section>
    );
}

function VersionRow({ version }: { version: PromptVersion }): JSX.Element {
    const [target, setTarget] = useState<State>(version.promotion_state);
    const [saving, setSaving] = useState(false);

    const onPromote = (): void => {
        setSaving(true);
        router.patch(
            `/admin/agent-config/prompts/${version.id}/promote`,
            { promotion_state: target },
            {
                preserveScroll: true,
                onFinish: () => setSaving(false),
            },
        );
    };

    return (
        <tr className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30" data-testid="prompt-row">
            <td className="px-3 py-2 font-mono text-xs text-stone-200">{version.version}</td>
            <td className="px-3 py-2">
                <span
                    className={`inline-block rounded border px-2 py-0.5 text-xs ${STATE_BADGE[version.promotion_state]}`}
                >
                    {version.promotion_state}
                </span>
            </td>
            <td className="px-3 py-2 font-mono text-xs text-stone-400">{version.promoted_at ?? '—'}</td>
            <td className="px-3 py-2 font-mono text-xs text-stone-400">{version.created_at}</td>
            <td className="max-w-md px-3 py-2 text-xs text-stone-400" title={version.notes ?? ''}>
                {version.notes ?? '—'}
            </td>
            <td className="px-3 py-2 text-right">
                <div className="flex items-center justify-end gap-2">
                    <select
                        value={target}
                        onChange={(e) => setTarget(e.target.value as State)}
                        className="rounded border border-stone-700 bg-stone-800 px-2 py-1 text-sm text-stone-100"
                    >
                        {STATES.map((s) => (
                            <option key={s} value={s}>
                                {s}
                            </option>
                        ))}
                    </select>
                    <button
                        type="button"
                        onClick={onPromote}
                        disabled={saving || target === version.promotion_state}
                        className="rounded bg-amber-500 px-3 py-1 text-xs font-medium text-stone-950 hover:bg-amber-400 disabled:opacity-50"
                        data-testid="promote-button"
                    >
                        {saving ? 'Saving…' : 'Promote'}
                    </button>
                </div>
            </td>
        </tr>
    );
}
