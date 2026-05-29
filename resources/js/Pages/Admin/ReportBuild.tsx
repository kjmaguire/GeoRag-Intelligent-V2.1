import type { JSX } from 'react';
import { useEffect, useState } from 'react';
import { Head, Link } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';

// `window.Echo` is declared in resources/js/bootstrap.ts as `any` for cross-page
// compatibility. The shape used here is captured locally for readability.
type ReverbEcho = {
    private(channel: string): { listen(event: string, cb: (e: unknown) => void): void };
    leave(channel: string): void;
};

type ProgressEvent = {
    build_id: string;
    stage: string;
    section_id?: string | null;
    message?: string | null;
    sections_completed?: number | null;
    sections_total?: number | null;
    timestamp?: string;
};

type SectionPlan = {
    section_id: string;
    title: string;
    template_slug: string;
    required_evidence_kinds: string[];
    map_kinds: string[];
    chart_kinds: string[];
};

type SectionDraft = {
    section_id: string;
    body_markdown: string;
    updated_at: string | null;
    updated_by_user_id: number | null;
};

type HistoryEntry = {
    audit_id: string;
    body_markdown: string;
    body_length: number;
    updated_at: string;
    updated_by_user_id: number | null;
};

type DiffLine = { kind: 'same' | 'add' | 'del'; text: string };

/**
 * Compact line-level diff using Myers-style LCS. Operates per-line on the
 * markdown body. Adequate for human review of revisions — not git-grade.
 * O(n*m) memory; capped by the 200k body_markdown ceiling so worst-case
 * ~4M cells, fine for an in-browser editor preview.
 */
function diffLines(before: string, after: string): DiffLine[] {
    const a = before.split('\n');
    const b = after.split('\n');
    const n = a.length;
    const m = b.length;
    // LCS table
    const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
    for (let i = 1; i <= n; i++) {
        for (let j = 1; j <= m; j++) {
            if (a[i - 1] === b[j - 1]) {
                lcs[i][j] = lcs[i - 1][j - 1] + 1;
            } else {
                lcs[i][j] = Math.max(lcs[i - 1][j], lcs[i][j - 1]);
            }
        }
    }
    // Backtrack
    const out: DiffLine[] = [];
    let i = n;
    let j = m;
    while (i > 0 && j > 0) {
        if (a[i - 1] === b[j - 1]) {
            out.unshift({ kind: 'same', text: a[i - 1] });
            i--; j--;
        } else if (lcs[i - 1][j] >= lcs[i][j - 1]) {
            out.unshift({ kind: 'del', text: a[i - 1] });
            i--;
        } else {
            out.unshift({ kind: 'add', text: b[j - 1] });
            j--;
        }
    }
    while (i > 0) { out.unshift({ kind: 'del', text: a[i - 1] }); i--; }
    while (j > 0) { out.unshift({ kind: 'add', text: b[j - 1] }); j--; }
    return out;
}

type Build = {
    build_id: string;
    report_type: string;
    workspace_id: string;
    project_id: string;
    requested_at: string;
    sections_planned: number;
    sections: SectionPlan[];
    drafts: Record<string, SectionDraft>;
    status: string;
};

type PageProps = { build: Build };

export default function ReportBuildShow({ build }: PageProps): JSX.Element {
    const [exporting, setExporting] = useState<boolean>(false);
    const [exportResult, setExportResult] = useState<Record<string, unknown> | null>(null);
    const [error, setError] = useState<string | null>(null);

    // Section-draft editor state: keyed by section_id.
    const [drafts, setDrafts] = useState<Record<string, string>>(() => {
        const initial: Record<string, string> = {};
        for (const s of build.sections) {
            initial[s.section_id] = build.drafts?.[s.section_id]?.body_markdown ?? '';
        }
        return initial;
    });
    const [savingSection, setSavingSection] = useState<string | null>(null);
    const [sectionResult, setSectionResult] = useState<Record<string, { ok: boolean; message: string }>>({});
    const [historyOpen, setHistoryOpen] = useState<string | null>(null);
    const [historyEntries, setHistoryEntries] = useState<Record<string, HistoryEntry[]>>({});
    const [loadingHistory, setLoadingHistory] = useState<string | null>(null);
    // Per-section diff selection: { sectionId: { from?: audit_id, to?: audit_id } }
    const [diffSelection, setDiffSelection] = useState<Record<string, { from?: string; to?: string }>>({});

    async function restoreRevision(sectionId: string, body: string): Promise<void> {
        // Copy the prior revision's body into the editor AND commit it as a
        // new draft (so the audit chain remembers the operator chose to
        // revert). Otherwise the page would silently lose the change on
        // refresh — operator must Save after Load, which is easy to miss.
        setDrafts(prev => ({ ...prev, [sectionId]: body }));
        setSavingSection(sectionId);
        try {
            const csrf = (document.querySelector('meta[name="csrf-token"]') as HTMLMetaElement | null)?.content ?? '';
            const resp = await fetch(
                `/admin/reports/${build.build_id}/sections/${encodeURIComponent(sectionId)}`,
                {
                    method: 'PUT',
                    credentials: 'include',
                    headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'application/json',
                        'X-CSRF-TOKEN': csrf,
                    },
                    body: JSON.stringify({
                        body_markdown: body,
                        updated_by_user_id: 1,
                    }),
                },
            );
            const respBody = await resp.json();
            if (resp.ok) {
                setSectionResult(prev => ({
                    ...prev,
                    [sectionId]: { ok: true, message: `Reverted at ${new Date(respBody.updated_at).toLocaleTimeString()}` },
                }));
                // Refresh history cache so the new audit row appears at the top.
                setHistoryEntries(prev => {
                    const { [sectionId]: _drop, ...rest } = prev;
                    return rest;
                });
                if (historyOpen === sectionId) {
                    await toggleHistory(sectionId);  // close
                    await toggleHistory(sectionId);  // re-open + re-fetch
                }
            } else {
                setSectionResult(prev => ({
                    ...prev,
                    [sectionId]: { ok: false, message: respBody.error ?? 'Restore failed.' },
                }));
            }
        } catch (err) {
            setSectionResult(prev => ({
                ...prev,
                [sectionId]: { ok: false, message: `Network error: ${(err as Error).message}` },
            }));
        } finally {
            setSavingSection(null);
        }
    }

    async function toggleHistory(sectionId: string): Promise<void> {
        if (historyOpen === sectionId) {
            setHistoryOpen(null);
            return;
        }
        setHistoryOpen(sectionId);
        if (historyEntries[sectionId] !== undefined) return; // already cached
        setLoadingHistory(sectionId);
        try {
            const resp = await fetch(
                `/admin/reports/${build.build_id}/sections/${encodeURIComponent(sectionId)}/history?limit=50`,
                { credentials: 'include', headers: { 'Accept': 'application/json' } },
            );
            if (resp.ok) {
                const body = await resp.json();
                setHistoryEntries(prev => ({ ...prev, [sectionId]: body.entries ?? [] }));
            } else {
                setHistoryEntries(prev => ({ ...prev, [sectionId]: [] }));
            }
        } catch {
            setHistoryEntries(prev => ({ ...prev, [sectionId]: [] }));
        } finally {
            setLoadingHistory(null);
        }
    }

    // Real-time build progress via Reverb (private-admin.reports.{build_id}).
    const [progress, setProgress] = useState<ProgressEvent[]>([]);
    const [liveStage, setLiveStage] = useState<string | null>(null);
    useEffect(() => {
        if (typeof window === 'undefined' || !window.Echo) return;
        const channelName = `admin.reports.${build.build_id}`;
        const ch = window.Echo.private(channelName);
        ch.listen('.ReportBuildProgress', (raw: unknown) => {
            const evt = raw as ProgressEvent;
            setLiveStage(evt.stage);
            setProgress(prev => [...prev.slice(-49), evt]);
        });
        return () => { window.Echo?.leave(channelName); };
    }, [build.build_id]);

    async function saveSection(sectionId: string): Promise<void> {
        setSavingSection(sectionId);
        setSectionResult(prev => {
            const { [sectionId]: _drop, ...rest } = prev;
            return rest;
        });
        try {
            const csrf = (document.querySelector('meta[name="csrf-token"]') as HTMLMetaElement | null)?.content ?? '';
            const resp = await fetch(
                `/admin/reports/${build.build_id}/sections/${encodeURIComponent(sectionId)}`,
                {
                    method: 'PUT',
                    credentials: 'include',
                    headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'application/json',
                        'X-CSRF-TOKEN': csrf,
                    },
                    body: JSON.stringify({
                        body_markdown: drafts[sectionId] ?? '',
                        updated_by_user_id: 1,
                    }),
                },
            );
            const body = await resp.json();
            if (resp.ok) {
                setSectionResult(prev => ({
                    ...prev,
                    [sectionId]: { ok: true, message: `Saved at ${new Date(body.updated_at).toLocaleTimeString()}` },
                }));
            } else {
                setSectionResult(prev => ({
                    ...prev,
                    [sectionId]: { ok: false, message: body.error ?? 'Save failed.' },
                }));
            }
        } catch (err) {
            setSectionResult(prev => ({
                ...prev,
                [sectionId]: { ok: false, message: `Network error: ${(err as Error).message}` },
            }));
        } finally {
            setSavingSection(null);
        }
    }

    async function triggerExport(): Promise<void> {
        setExporting(true);
        setError(null);
        setExportResult(null);
        try {
            const csrf = (document.querySelector('meta[name="csrf-token"]') as HTMLMetaElement | null)?.content ?? '';
            const resp = await fetch('/admin/reports/export', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'X-CSRF-TOKEN': csrf,
                },
                body: JSON.stringify({
                    workspace_id: build.workspace_id,
                    project_id: build.project_id,
                    report_type: build.report_type,
                    requested_by_user_id: 1,
                }),
            });
            const body = await resp.json();
            if (resp.ok) {
                setExportResult(body);
            } else {
                setError(body.error ?? 'Export failed.');
            }
        } catch (err) {
            setError(`Network error: ${(err as Error).message}`);
        } finally {
            setExporting(false);
        }
    }

    return (
        <AppLayout>
            <Head title={`Build ${build.build_id.slice(0, 8)}`} />
            <div className="px-6 py-4">
                <div className="mb-4">
                    <Link href="/admin/reports" className="text-blue-600 text-sm hover:underline">
                        ← All builds
                    </Link>
                </div>
                <h1 className="text-2xl font-semibold mb-2">Report Build</h1>
                <p className="text-sm text-gray-600 mb-1">
                    Build <code className="font-mono">{build.build_id}</code> · Type <strong>{build.report_type}</strong> · Status {build.status}
                </p>
                <p className="text-sm text-gray-600 mb-4">
                    Workspace <code className="font-mono text-xs">{build.workspace_id}</code> · Project <code className="font-mono text-xs">{build.project_id}</code> · Requested {new Date(build.requested_at).toLocaleString()}
                </p>

                {(liveStage || progress.length > 0) && (
                    <div className="mb-4 p-3 bg-indigo-50 border border-indigo-200 rounded">
                        <div className="flex items-baseline justify-between">
                            <h2 className="text-md font-semibold text-indigo-900">
                                Live progress — {liveStage ?? 'idle'}
                            </h2>
                            <span className="text-xs text-indigo-700">
                                {progress.length} event{progress.length !== 1 ? 's' : ''}
                            </span>
                        </div>
                        {progress.length > 0 && (
                            <ul className="mt-2 text-xs font-mono space-y-0.5 max-h-40 overflow-y-auto">
                                {progress.map((p, i) => (
                                    <li key={i} className="text-indigo-900">
                                        <span className="text-indigo-500">
                                            {p.timestamp ? new Date(p.timestamp).toLocaleTimeString() : ''}
                                        </span>{' '}
                                        <span className="font-semibold">{p.stage}</span>
                                        {p.section_id && <span> · {p.section_id}</span>}
                                        {p.sections_completed != null && p.sections_total != null && (
                                            <span> · {p.sections_completed}/{p.sections_total}</span>
                                        )}
                                        {p.message && <span className="text-indigo-700"> — {p.message}</span>}
                                    </li>
                                ))}
                            </ul>
                        )}
                    </div>
                )}

                <div className="mb-4 p-3 bg-white border rounded">
                    <h2 className="text-md font-semibold mb-2">Render this report</h2>
                    <p className="text-sm text-gray-600 mb-2">
                        Triggers the §15 generate_report workflow — drafts every
                        section, runs the §29 export-compliance gates, returns
                        artifact URIs.
                    </p>
                    <button
                        type="button"
                        onClick={triggerExport}
                        disabled={exporting}
                        className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:bg-gray-300"
                    >
                        {exporting ? 'Rendering…' : 'Render report'}
                    </button>
                    {error && (
                        <div className="mt-2 p-2 bg-red-50 text-red-800 text-sm rounded">{error}</div>
                    )}
                    {exportResult && (
                        <pre className="mt-2 p-2 bg-gray-50 text-xs rounded max-h-72 overflow-auto">
                            {JSON.stringify(exportResult, null, 2)}
                        </pre>
                    )}
                </div>

                <h2 className="text-lg font-semibold mt-4 mb-2">Planned sections ({build.sections.length})</h2>
                <p className="text-xs text-gray-500 mb-2">
                    Edit the draft body markdown per section. Saves are
                    audit-anchored as <code>report.build.section.drafted</code>
                    events; the latest draft per section is rendered when the
                    §15 workflow runs (operator override).
                </p>
                <ul className="space-y-3">
                    {build.sections.map((s, idx) => {
                        const existing = build.drafts?.[s.section_id];
                        const status = sectionResult[s.section_id];
                        return (
                            <li key={s.section_id} className="p-3 border rounded bg-white">
                                <div className="flex justify-between">
                                    <h3 className="font-medium">
                                        {idx + 1}. {s.title}
                                    </h3>
                                    <span className="text-xs text-gray-500 font-mono">{s.template_slug}</span>
                                </div>
                                {s.required_evidence_kinds.length > 0 && (
                                    <p className="text-xs text-gray-600 mt-1">
                                        Evidence: {s.required_evidence_kinds.join(', ')}
                                    </p>
                                )}
                                {[...s.map_kinds, ...s.chart_kinds].length > 0 && (
                                    <p className="text-xs text-gray-600">
                                        Visuals: {[...s.map_kinds, ...s.chart_kinds].join(', ')}
                                    </p>
                                )}
                                <textarea
                                    className="block w-full mt-2 p-2 border rounded text-sm font-mono"
                                    rows={6}
                                    value={drafts[s.section_id] ?? ''}
                                    onChange={e => setDrafts(prev => ({ ...prev, [s.section_id]: e.target.value }))}
                                    placeholder="Draft body markdown — cite source_chunk_ids per §04i."
                                />
                                <div className="flex items-center gap-2 mt-2">
                                    <button
                                        type="button"
                                        onClick={() => saveSection(s.section_id)}
                                        disabled={savingSection === s.section_id}
                                        className="px-3 py-1 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 disabled:bg-gray-300"
                                    >
                                        {savingSection === s.section_id ? 'Saving…' : 'Save draft'}
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => toggleHistory(s.section_id)}
                                        className="px-3 py-1 text-sm bg-gray-100 border rounded hover:bg-gray-200"
                                    >
                                        {historyOpen === s.section_id ? 'Hide history' : 'History'}
                                    </button>
                                    {existing?.updated_at && (
                                        <span className="text-xs text-gray-500">
                                            Last saved {new Date(existing.updated_at).toLocaleString()}
                                            {existing.updated_by_user_id != null && ` by #${existing.updated_by_user_id}`}
                                        </span>
                                    )}
                                    {status && (
                                        <span className={`text-xs ${status.ok ? 'text-green-700' : 'text-red-700'}`}>
                                            {status.message}
                                        </span>
                                    )}
                                </div>
                                {historyOpen === s.section_id && (() => {
                                    const entries = historyEntries[s.section_id] ?? [];
                                    const sel = diffSelection[s.section_id] ?? {};
                                    const findEntry = (id?: string) => entries.find(e => e.audit_id === id);
                                    const fromEntry = findEntry(sel.from);
                                    const toEntry = findEntry(sel.to);
                                    const diff = fromEntry && toEntry
                                        ? diffLines(fromEntry.body_markdown, toEntry.body_markdown)
                                        : null;
                                    return (
                                    <div className="mt-2 p-2 bg-slate-50 border rounded text-xs">
                                        {loadingHistory === s.section_id && <p className="text-gray-500">Loading history…</p>}
                                        {loadingHistory !== s.section_id && entries.length === 0 && (
                                            <p className="text-gray-500">No saved revisions yet.</p>
                                        )}
                                        {entries.length >= 2 && (
                                            <div className="mb-2 p-1 bg-white border rounded flex items-center gap-2 flex-wrap">
                                                <span className="text-gray-700 font-medium">Diff:</span>
                                                <label className="flex items-center gap-1">
                                                    <span className="text-gray-600">from</span>
                                                    <select
                                                        className="p-0.5 border rounded text-xs"
                                                        value={sel.from ?? ''}
                                                        onChange={e => setDiffSelection(prev => ({
                                                            ...prev,
                                                            [s.section_id]: { ...(prev[s.section_id] ?? {}), from: e.target.value || undefined },
                                                        }))}
                                                    >
                                                        <option value="">— pick revision —</option>
                                                        {entries.map(e => (
                                                            <option key={e.audit_id} value={e.audit_id}>
                                                                {new Date(e.updated_at).toLocaleString()} · {e.body_length}ch
                                                            </option>
                                                        ))}
                                                    </select>
                                                </label>
                                                <label className="flex items-center gap-1">
                                                    <span className="text-gray-600">to</span>
                                                    <select
                                                        className="p-0.5 border rounded text-xs"
                                                        value={sel.to ?? ''}
                                                        onChange={e => setDiffSelection(prev => ({
                                                            ...prev,
                                                            [s.section_id]: { ...(prev[s.section_id] ?? {}), to: e.target.value || undefined },
                                                        }))}
                                                    >
                                                        <option value="">— pick revision —</option>
                                                        {entries.map(e => (
                                                            <option key={e.audit_id} value={e.audit_id}>
                                                                {new Date(e.updated_at).toLocaleString()} · {e.body_length}ch
                                                            </option>
                                                        ))}
                                                    </select>
                                                </label>
                                                {diff && (
                                                    <span className="text-gray-600">
                                                        +{diff.filter(d => d.kind === 'add').length} / -{diff.filter(d => d.kind === 'del').length}
                                                    </span>
                                                )}
                                                {(sel.from || sel.to) && (
                                                    <button
                                                        type="button"
                                                        className="text-blue-600 hover:underline"
                                                        onClick={() => setDiffSelection(prev => ({ ...prev, [s.section_id]: {} }))}
                                                    >
                                                        clear
                                                    </button>
                                                )}
                                            </div>
                                        )}
                                        {diff && (
                                            <pre className="mb-2 p-1 bg-white border max-h-72 overflow-auto font-mono">
                                                {diff.map((d, idx) => (
                                                    <div
                                                        key={idx}
                                                        className={
                                                            d.kind === 'add' ? 'bg-green-100 text-green-900' :
                                                            d.kind === 'del' ? 'bg-red-100 text-red-900' :
                                                                              'text-gray-700'
                                                        }
                                                    >
                                                        {d.kind === 'add' ? '+ ' : d.kind === 'del' ? '- ' : '  '}
                                                        {d.text || ' '}
                                                    </div>
                                                ))}
                                            </pre>
                                        )}
                                        {entries.map(h => (
                                            <div key={h.audit_id} className="py-1 border-b last:border-b-0">
                                                <div className="flex justify-between">
                                                    <span className="font-mono text-gray-700">
                                                        {new Date(h.updated_at).toLocaleString()}
                                                        {h.updated_by_user_id != null && ` · #${h.updated_by_user_id}`}
                                                        {' · '}{h.body_length} chars
                                                    </span>
                                                    <div className="flex gap-1">
                                                        <button
                                                            type="button"
                                                            onClick={() => setDrafts(prev => ({ ...prev, [s.section_id]: h.body_markdown }))}
                                                            className="px-2 py-0.5 bg-slate-100 border border-slate-300 rounded hover:bg-slate-200"
                                                            title="Load this revision into the editor (does NOT save)"
                                                        >
                                                            Load
                                                        </button>
                                                        <button
                                                            type="button"
                                                            onClick={() => restoreRevision(s.section_id, h.body_markdown)}
                                                            disabled={savingSection === s.section_id}
                                                            className="px-2 py-0.5 bg-amber-100 border border-amber-300 rounded hover:bg-amber-200 disabled:bg-gray-200"
                                                            title="Commit this revision as a new save (audit-anchored revert)"
                                                        >
                                                            Restore
                                                        </button>
                                                    </div>
                                                </div>
                                                <pre className="mt-1 p-1 bg-white border max-h-32 overflow-auto whitespace-pre-wrap">
                                                    {h.body_markdown.slice(0, 400)}{h.body_markdown.length > 400 ? '…' : ''}
                                                </pre>
                                            </div>
                                        ))}
                                    </div>
                                    );
                                })()}
                            </li>
                        );
                    })}
                </ul>
            </div>
        </AppLayout>
    );
}
