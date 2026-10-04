import { useCallback, useEffect, useMemo, useState } from 'react';
import { useToast } from '@/Components/Foundry/ToastHost';

/**
 * Freshness line + admin-only "Sync now" for the Public Geo page.
 *
 *   GET  /api/v1/public-geoscience/sync-status  → rows + latest last_seen_at
 *   POST /api/v1/public-geoscience/sync         → enqueue public_geo_sync (202)
 *
 * The button renders only for admins (the shared `auth.user.is_admin` prop),
 * but that is presentation: the POST is authorised server-side against the
 * `admin` gate, so hiding it is not the control. A 202 toasts the Hatchet
 * run id; a 429 (clicked again inside the cooldown) toasts the run the first
 * click started; anything else toasts the server's message.
 */

interface SyncStatusRow {
    layer: string;
    jurisdiction_code: string;
    rows: number;
    last_seen_at: string | null;
}

interface SyncStatus {
    layers: SyncStatusRow[];
    last_seen_at: string | null;
}

interface Props {
    isAdmin: boolean;
}

function getCsrf(): string | null {
    return document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') ?? null;
}

const JSON_HEADERS = { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' };

export function formatFreshness(status: SyncStatus | null): string | null {
    if (!status) return null;
    // Guard the shape, not just null: this renders in the page header, so a
    // malformed body must degrade to 'Not synced yet', never throw.
    if (!Array.isArray(status.layers) || !status.layers.length || !status.last_seen_at) return 'Not synced yet';
    const byJurisdiction = new Map<string, number>();
    for (const row of status.layers) {
        byJurisdiction.set(row.jurisdiction_code, (byJurisdiction.get(row.jurisdiction_code) ?? 0) + row.rows);
    }
    const counts = Array.from(byJurisdiction.entries())
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([code, n]) => `${code} ${n.toLocaleString()}`)
        .join(' · ');
    const when = new Date(status.last_seen_at);
    const whenText = Number.isNaN(when.getTime()) ? status.last_seen_at : when.toLocaleString();
    return `Last synced ${whenText} · ${counts}`;
}

export default function PublicGeoSyncControls({ isAdmin }: Props) {
    const { pushToast } = useToast();
    const [status, setStatus] = useState<SyncStatus | null>(null);
    const [submitting, setSubmitting] = useState(false);

    useEffect(() => {
        const controller = new AbortController();
        fetch('/api/v1/public-geoscience/sync-status', {
            credentials: 'same-origin',
            headers: JSON_HEADERS,
            signal: controller.signal,
        })
            .then((res) => (res.ok ? (res.json() as Promise<SyncStatus>) : null))
            .then((body) => {
                if (body) setStatus(body);
            })
            // Freshness is a nicety; its failure must not disturb the map.
            .catch(() => undefined);
        return () => controller.abort();
    }, []);

    const syncNow = useCallback(async () => {
        setSubmitting(true);
        try {
            const csrf = getCsrf();
            const res = await fetch('/api/v1/public-geoscience/sync', {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    ...JSON_HEADERS,
                    'Content-Type': 'application/json',
                    ...(csrf ? { 'X-CSRF-TOKEN': csrf } : {}),
                },
                body: JSON.stringify({}),
            });
            const body = (await res.json().catch(() => ({}))) as {
                workflow_run_id?: string | null;
                feeds?: number;
                message?: string;
            };
            if (res.status === 202 && body.workflow_run_id) {
                pushToast({
                    title: 'Public geo sync queued',
                    detail: `Sync started · ${body.feeds ?? '?'} feeds. It runs for a while; counts update when it finishes.`,
                    tone: 'accent',
                    durationMs: 15000,
                });
            } else if (res.status === 429) {
                pushToast({
                    title: 'A sync was just triggered',
                    detail: body.workflow_run_id ? 'A sync is already queued — results will update when it finishes.' : body.message,
                    tone: 'warn',
                });
            } else {
                pushToast({
                    title: 'Sync could not be started',
                    detail: body.message ?? `HTTP ${res.status}`,
                    tone: 'warn',
                });
            }
        } catch (err) {
            pushToast({
                title: 'Sync could not be started',
                detail: err instanceof Error ? err.message : String(err),
                tone: 'warn',
            });
        } finally {
            setSubmitting(false);
        }
    }, [pushToast]);

    const freshness = useMemo(() => formatFreshness(status), [status]);

    return (
        <div className="flex items-center gap-3 text-[10px] font-mono" style={{ color: 'var(--fg-3)' }}>
            {freshness && <span data-testid="pg-freshness">{freshness}</span>}
            {isAdmin && (
                <button
                    type="button"
                    onClick={() => void syncNow()}
                    disabled={submitting}
                    className="border rounded px-2 py-1 uppercase tracking-wider disabled:opacity-50"
                    style={{ borderColor: 'var(--line-2)', color: 'var(--fg-1)' }}
                    title="Queue a public_geo_sync run now (admin)"
                >
                    {submitting ? 'Queuing…' : 'Sync now'}
                </button>
            )}
        </div>
    );
}
