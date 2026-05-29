import type { JSX } from 'react';
import { useState } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import TrgZoneMap from '../../Components/Admin/TrgZoneMap';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

/**
 * /admin/target-recommendation/runs/{run_id} — Phase H4 §8 UI.
 *
 * Cockpit + R5 QP sign-off ceremony for a single TRG run. The
 * sign-off path enforces the §29.6 invariant: signed_off requires
 * credential_verified=true.
 */

type RankedTarget = {
    zone_id: string;
    rank: number;
    aggregate_score: number;
    aggregate_uncertainty: number | null;
    explanation_markdown: string | null;
    factor_count: number;
};

type Run = {
    run_id: string;
    workspace_id: string;
    project_id: string;
    project_name: string | null;
    created_at: string | null;
    ranked_targets: RankedTarget[];
    target_model_slug: string | null;
    scoring_kind: string | null;
    map_layer_uris: Record<string, string>;
    sign_off_status: 'pending' | 'signed_off' | 'rejected' | 'modified';
    last_decision: {
        decision: string;
        rationale: string;
        qp_user_id: number;
        qp_signature_method: string;
        signed_at: string | null;
    } | null;
};

type PageProps = { run: Run };

type Decision = 'accepted' | 'modified' | 'rejected' | 'signed_off';

const DECISIONS: { value: Decision; label: string; description: string }[] = [
    { value: 'accepted',   label: 'Accept',     description: 'Targets ready for action; no changes.' },
    { value: 'modified',   label: 'Modify',     description: 'Accept with operator-applied edits.' },
    { value: 'rejected',   label: 'Reject',     description: 'Do not act on these targets.' },
    { value: 'signed_off', label: 'Sign off',   description: 'R5 — final QP attestation. Requires credential verified.' },
];

export default function TargetRecommendationCockpit({ run }: PageProps): JSX.Element {
    // Phase 2 real-time push — score_targets broadcasts to the per-run
    // channel admin.target-run.{run_id} on completion (matches the
    // admin.reports.{build_id} precedent). This complements the
    // router.reload(['run']) the signoff handler already calls on the
    // QP sign-off ceremony — now the cockpit also lights up when the
    // workflow finishes upstream.
    useAdminSurfaceUpdated('target-run', run.run_id, () => {
        router.reload({ only: ['run'] });
    });

    const [selectedTarget, setSelectedTarget] = useState<RankedTarget | null>(
        run.ranked_targets[0] ?? null,
    );
    const [qpUserId, setQpUserId] = useState<number>(0);
    const [qpCredentialId, setQpCredentialId] = useState<string>('');
    const [decision, setDecision] = useState<Decision>('accepted');
    const [rationale, setRationale] = useState<string>('');
    const [signatureMethod, setSignatureMethod] = useState<string>('digital_token');
    const [credentialVerified, setCredentialVerified] = useState<boolean>(false);
    const [submitting, setSubmitting] = useState<boolean>(false);
    const [submitResult, setSubmitResult] = useState<{ ok: boolean; message: string } | null>(null);

    async function submitSignoff(): Promise<void> {
        if (!selectedTarget) return;
        if (!rationale.trim()) {
            setSubmitResult({ ok: false, message: 'Rationale is required.' });
            return;
        }
        setSubmitting(true);
        setSubmitResult(null);

        try {
            const csrfToken = (document.querySelector(
                'meta[name="csrf-token"]',
            ) as HTMLMetaElement | null)?.content ?? '';
            const resp = await fetch(
                `/admin/target-recommendation/runs/${run.run_id}/signoff`,
                {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'application/json',
                        'X-CSRF-TOKEN': csrfToken,
                    },
                    body: JSON.stringify({
                        target_id: selectedTarget.zone_id,
                        qp_user_id: qpUserId,
                        qp_credential_id: qpCredentialId,
                        decision,
                        rationale,
                        qp_signature_method: signatureMethod,
                        credential_verified: credentialVerified,
                    }),
                },
            );
            const body = await resp.json();
            if (resp.ok) {
                setSubmitResult({
                    ok: true,
                    message: `Sign-off recorded — review_id=${body.review_id}`,
                });
                router.reload({ only: ['run'] });
            } else {
                setSubmitResult({
                    ok: false,
                    message: body.error ?? 'Sign-off failed.',
                });
            }
        } catch (err) {
            setSubmitResult({
                ok: false,
                message: `Network error: ${(err as Error).message}`,
            });
        } finally {
            setSubmitting(false);
        }
    }

    const signedOffDisabled =
        decision === 'signed_off' && !credentialVerified;

    return (
        <AppLayout>
            <Head title={`Run ${run.run_id.slice(0, 8)}`} />
            <div className="px-6 py-4">
                <div className="mb-4">
                    <Link
                        href="/admin/target-recommendation/runs"
                        className="text-blue-600 text-sm hover:underline"
                    >
                        ← All runs
                    </Link>
                </div>

                <h1 className="text-2xl font-semibold">
                    Target Recommendation Cockpit
                </h1>
                <p className="text-sm text-gray-600 mb-4">
                    Run <code className="font-mono">{run.run_id}</code> ·{' '}
                    Project <span className="font-medium">{run.project_name ?? run.project_id}</span> ·{' '}
                    Model <span className="font-medium">{run.target_model_slug ?? '—'}</span> ({run.scoring_kind ?? '—'})
                </p>

                {run.last_decision && (
                    <div className="mb-4 p-3 bg-blue-50 border border-blue-200 rounded text-sm">
                        <strong>Latest decision:</strong> {run.last_decision.decision} by
                        QP #{run.last_decision.qp_user_id} ({run.last_decision.qp_signature_method}){' '}
                        — {run.last_decision.rationale}
                    </div>
                )}

                {/* MapLibre panel — zones coloured by aggregate_score, click to select. */}
                <div className="mb-4">
                    <TrgZoneMap
                        runId={run.run_id}
                        selectedZoneId={selectedTarget?.zone_id ?? null}
                        onZoneClick={(zid) => {
                            const t = run.ranked_targets.find(r => r.zone_id === zid);
                            if (t) setSelectedTarget(t);
                        }}
                    />
                </div>

                <div className="grid grid-cols-12 gap-4">
                    {/* Targets list */}
                    <div className="col-span-5">
                        <h2 className="text-lg font-semibold mb-2">
                            Ranked targets ({run.ranked_targets.length})
                        </h2>
                        <ul className="space-y-1 max-h-[60vh] overflow-y-auto">
                            {run.ranked_targets.map(t => (
                                <li key={t.zone_id}>
                                    <button
                                        type="button"
                                        onClick={() => setSelectedTarget(t)}
                                        className={`w-full text-left p-2 rounded border ${
                                            selectedTarget?.zone_id === t.zone_id
                                                ? 'border-blue-500 bg-blue-50'
                                                : 'border-gray-200 hover:bg-gray-50'
                                        }`}
                                    >
                                        <div className="flex justify-between">
                                            <span className="font-medium">
                                                #{t.rank} · {t.zone_id.slice(0, 8)}…
                                            </span>
                                            <span className="text-gray-600 font-mono text-sm">
                                                {t.aggregate_score.toFixed(3)}
                                            </span>
                                        </div>
                                        <div className="text-xs text-gray-500">
                                            {t.factor_count} factor(s)
                                            {t.aggregate_uncertainty != null
                                                ? ` · σ ${t.aggregate_uncertainty.toFixed(2)}`
                                                : ''}
                                        </div>
                                    </button>
                                </li>
                            ))}
                        </ul>
                    </div>

                    {/* Selected target + sign-off form */}
                    <div className="col-span-7">
                        {selectedTarget ? (
                            <>
                                <h2 className="text-lg font-semibold mb-2">
                                    Rationale — #{selectedTarget.rank}
                                </h2>
                                <pre className="p-3 bg-gray-50 rounded text-sm whitespace-pre-wrap mb-4 max-h-[40vh] overflow-y-auto">
                                    {selectedTarget.explanation_markdown ?? 'No rationale recorded.'}
                                </pre>

                                <h3 className="text-md font-semibold mb-2 mt-4">
                                    R5 QP sign-off
                                </h3>
                                <div className="p-3 border rounded bg-white">
                                    <div className="grid grid-cols-2 gap-3">
                                        <label className="text-sm">
                                            QP user id
                                            <input
                                                type="number"
                                                className="block w-full mt-1 p-1.5 border rounded"
                                                value={qpUserId || ''}
                                                onChange={e => setQpUserId(parseInt(e.target.value, 10) || 0)}
                                            />
                                        </label>
                                        <label className="text-sm">
                                            QP credential id
                                            <input
                                                type="text"
                                                className="block w-full mt-1 p-1.5 border rounded"
                                                value={qpCredentialId}
                                                onChange={e => setQpCredentialId(e.target.value)}
                                                placeholder="APGO-12345"
                                            />
                                        </label>
                                        <label className="text-sm">
                                            Decision
                                            <select
                                                className="block w-full mt-1 p-1.5 border rounded"
                                                value={decision}
                                                onChange={e => setDecision(e.target.value as Decision)}
                                            >
                                                {DECISIONS.map(d => (
                                                    <option key={d.value} value={d.value}>
                                                        {d.label}
                                                    </option>
                                                ))}
                                            </select>
                                            <span className="text-xs text-gray-500 mt-1 block">
                                                {DECISIONS.find(d => d.value === decision)?.description}
                                            </span>
                                        </label>
                                        <label className="text-sm">
                                            Signature method
                                            <select
                                                className="block w-full mt-1 p-1.5 border rounded"
                                                value={signatureMethod}
                                                onChange={e => setSignatureMethod(e.target.value)}
                                            >
                                                <option value="digital_token">Digital token</option>
                                                <option value="wet_signature">Wet signature</option>
                                                <option value="manual">Manual</option>
                                            </select>
                                        </label>
                                    </div>
                                    <label className="text-sm block mt-3">
                                        Rationale (required)
                                        <textarea
                                            className="block w-full mt-1 p-1.5 border rounded"
                                            rows={3}
                                            value={rationale}
                                            onChange={e => setRationale(e.target.value)}
                                            placeholder="Why this decision? Cite the §04i layers + §29 gates inspected."
                                        />
                                    </label>
                                    {decision === 'signed_off' && (
                                        <label className="text-sm block mt-3">
                                            <input
                                                type="checkbox"
                                                checked={credentialVerified}
                                                onChange={e => setCredentialVerified(e.target.checked)}
                                                className="mr-2"
                                            />
                                            QP credential verified (staffed-ops gate per §29.6.1).
                                            Required for <strong>signed_off</strong>.
                                        </label>
                                    )}
                                    <button
                                        type="button"
                                        onClick={submitSignoff}
                                        disabled={submitting || signedOffDisabled}
                                        className="mt-3 px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
                                    >
                                        {submitting ? 'Recording…' : 'Record decision'}
                                    </button>
                                    {submitResult && (
                                        <div
                                            className={`mt-3 p-2 rounded text-sm ${
                                                submitResult.ok
                                                    ? 'bg-green-50 text-green-800'
                                                    : 'bg-red-50 text-red-800'
                                            }`}
                                        >
                                            {submitResult.message}
                                        </div>
                                    )}
                                </div>
                            </>
                        ) : (
                            <p className="text-gray-500">No target selected.</p>
                        )}
                    </div>
                </div>
            </div>
        </AppLayout>
    );
}
