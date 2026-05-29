import { useState } from 'react';
import type { JSX } from 'react';
import { Head, Link, router, usePage } from '@inertiajs/react';
import AppLayout from '../../../Layouts/AppLayout';

/**
 * /admin/agent-config/pins — Phase 0 Step 5.2.
 *
 * Lists workspace.agent_prompt_pins. Each row binds an agent to a
 * specific prompt_version_id (NULL = use whichever version is currently
 * promoted to production for that prompt_id). Saving writes the row +
 * an audit_ledger entry inside one DB transaction.
 */

type State = 'draft' | 'staging' | 'production' | 'deprecated';

interface AgentPin {
    agent_name: string;
    prompt_id: string;
    prompt_version_id: string | null;
    pinned_version_label: string | null;
    pinned_promotion_state: State | null;
    pinned_at: string | null;
    pinned_by: number | null;
    updated_at: string;
}

interface AvailableVersion {
    id: string;
    version: string;
    promotion_state: State;
}

interface PageProps {
    pins: AgentPin[];
    available_versions: Record<string, AvailableVersion[]>;
    [key: string]: unknown;
}

interface FlashProps {
    flash?: { success?: string };
    [key: string]: unknown;
}

export default function Pins({ pins, available_versions }: PageProps): JSX.Element {
    const { props } = usePage<FlashProps>();
    const flash = props.flash?.success ?? null;

    return (
        <AppLayout>
            <Head title="Agent prompt pins — Admin" />
            <div className="min-h-screen bg-stone-950 text-stone-100">
                <div className="mx-auto max-w-7xl px-6 py-8" data-testid="agent-config-pins">
                    <Link
                        href="/dashboard"
                        className="mb-4 inline-block text-sm text-stone-400 hover:text-amber-300"
                    >
                        ← Back to dashboard
                    </Link>

                    <header className="mb-6">
                        <h1 className="text-2xl font-semibold text-stone-50">Agent prompt pins</h1>
                        <p className="mt-1 text-sm text-stone-400">
                            Per-agent prompt-version pin from{' '}
                            <code className="text-stone-300">workspace.agent_prompt_pins</code>. NULL = wrapper falls
                            through to the production-promoted prompt at invocation time.
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

                    <div className="overflow-x-auto rounded border border-stone-800 bg-stone-900">
                        <table className="w-full text-left text-sm" data-testid="pins-table">
                            <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                <tr>
                                    <th className="px-3 py-2">Agent</th>
                                    <th className="px-3 py-2">Prompt ID</th>
                                    <th className="px-3 py-2">Pinned version</th>
                                    <th className="px-3 py-2">Pinned at</th>
                                    <th className="px-3 py-2 text-right">Update</th>
                                </tr>
                            </thead>
                            <tbody>
                                {pins.length === 0 && (
                                    <tr>
                                        <td colSpan={5} className="px-3 py-8 text-center text-stone-500">
                                            No pins seeded.
                                        </td>
                                    </tr>
                                )}
                                {pins.map((pin) => (
                                    <PinRow
                                        key={pin.agent_name}
                                        pin={pin}
                                        available={available_versions[pin.prompt_id] ?? []}
                                    />
                                ))}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </AppLayout>
    );
}

function PinRow({ pin, available }: { pin: AgentPin; available: AvailableVersion[] }): JSX.Element {
    const [versionId, setVersionId] = useState<string>(pin.prompt_version_id ?? '');
    const [saving, setSaving] = useState(false);

    const onSave = (): void => {
        setSaving(true);
        router.patch(
            `/admin/agent-config/pins/${encodeURIComponent(pin.agent_name)}`,
            { prompt_version_id: versionId === '' ? null : versionId },
            {
                preserveScroll: true,
                onFinish: () => setSaving(false),
            },
        );
    };

    return (
        <tr className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30" data-testid="pin-row">
            <td className="px-3 py-2 text-stone-200">{pin.agent_name}</td>
            <td className="px-3 py-2 font-mono text-xs text-stone-300">{pin.prompt_id}</td>
            <td className="px-3 py-2 font-mono text-xs text-stone-400">
                {pin.pinned_version_label ?? <span className="text-stone-600">— (use production)</span>}
            </td>
            <td className="px-3 py-2 font-mono text-xs text-stone-500">{pin.pinned_at ?? '—'}</td>
            <td className="px-3 py-2 text-right">
                <div className="flex items-center justify-end gap-2">
                    <select
                        value={versionId}
                        onChange={(e) => setVersionId(e.target.value)}
                        className="rounded border border-stone-700 bg-stone-800 px-2 py-1 text-sm text-stone-100"
                    >
                        <option value="">— unpin (use production) —</option>
                        {available.map((v) => (
                            <option key={v.id} value={v.id}>
                                {v.version} [{v.promotion_state}]
                            </option>
                        ))}
                    </select>
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
            </td>
        </tr>
    );
}
