import type { JSX } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

/**
 * /admin/what-changed — §9.9 What-Changed digest viewer.
 *
 * Lists recent what_changed_detector audit anchors with their
 * per-workspace delta payloads.
 */

type Run = {
    run_id: string;
    workspace_id: string | null;
    created_at: string;
    payload: {
        new_ingestion_count?: number;
        new_public_record_count?: number;
        updated_public_record_count?: number;
        new_claim_count?: number;
        target_score_shift_count?: number;
        new_decision_count?: number;
        new_hypothesis_count?: number;
        new_support_ticket_count?: number;
        total_audit_anchors_in_window?: number;
        window_start?: string;
        window_end?: string;
        [k: string]: unknown;
    };
};

type PageProps = { runs: Run[]; fastapi_error: string | null };

function num(v: unknown): string {
    if (typeof v !== 'number') return '—';
    return v.toLocaleString();
}

export default function WhatChanged({ runs, fastapi_error }: PageProps): JSX.Element {
    // Phase 5 real-time push — what_changed_detector + what_changed_weekly
    // both broadcast `what-changed` on completion.
    useAdminSurfaceUpdated('what-changed', null, () => {
        router.reload({ only: ['runs'] });
    });

    return (
        <AppLayout>
            <Head title="What-Changed Digest" />
            <div className="px-6 py-4">
                <h1 className="text-2xl font-semibold mb-2">What-Changed Digest</h1>
                <p className="text-sm text-gray-600 mb-4">
                    §9.9 / §20.2 — per-workspace delta detector. Each run
                    captures the workspace's state change inside its
                    window: new ingestions, new public records, target
                    score shifts, new claims/decisions/hypotheses,
                    support tickets.
                </p>

                {fastapi_error && (
                    <div className="mb-3 p-3 bg-red-50 text-red-800 text-sm rounded">
                        Could not reach FastAPI: {fastapi_error}
                    </div>
                )}

                <table className="w-full text-sm border-collapse">
                    <thead>
                        <tr className="bg-gray-50 text-left">
                            <th className="py-2 px-2">Workspace</th>
                            <th className="py-2 px-2 text-right">Ingestions</th>
                            <th className="py-2 px-2 text-right">Public</th>
                            <th className="py-2 px-2 text-right">Claims</th>
                            <th className="py-2 px-2 text-right">Decisions</th>
                            <th className="py-2 px-2 text-right">Hypotheses</th>
                            <th className="py-2 px-2 text-right">Score shifts</th>
                            <th className="py-2 px-2 text-right">Tickets</th>
                            <th className="py-2 px-2">Window</th>
                        </tr>
                    </thead>
                    <tbody>
                        {runs.length === 0 && (
                            <tr>
                                <td colSpan={9} className="py-6 text-center text-gray-500">
                                    No what_changed runs yet.
                                </td>
                            </tr>
                        )}
                        {runs.map(r => (
                            <tr key={r.run_id} className="border-b hover:bg-gray-50">
                                <td className="py-2 px-2 font-mono text-xs">
                                    {r.workspace_id ? r.workspace_id.slice(0, 8) + '…' : '—'}
                                </td>
                                <td className="py-2 px-2 text-right">{num(r.payload.new_ingestion_count)}</td>
                                <td className="py-2 px-2 text-right">
                                    {num(r.payload.new_public_record_count)}
                                    {r.payload.updated_public_record_count
                                        ? <span className="text-gray-500"> (+{num(r.payload.updated_public_record_count)} upd)</span>
                                        : null}
                                </td>
                                <td className="py-2 px-2 text-right">{num(r.payload.new_claim_count)}</td>
                                <td className="py-2 px-2 text-right">{num(r.payload.new_decision_count)}</td>
                                <td className="py-2 px-2 text-right">{num(r.payload.new_hypothesis_count)}</td>
                                <td className="py-2 px-2 text-right">{num(r.payload.target_score_shift_count)}</td>
                                <td className="py-2 px-2 text-right">{num(r.payload.new_support_ticket_count)}</td>
                                <td className="py-2 px-2 text-xs text-gray-600">
                                    {r.payload.window_start && r.payload.window_end
                                        ? `${new Date(r.payload.window_start as string).toLocaleDateString()} → ${new Date(r.payload.window_end as string).toLocaleDateString()}`
                                        : '—'}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </AppLayout>
    );
}
