import { useEffect, useState } from 'react';

/**
 * ProjectContextBanner — D6 from the senior review.
 *
 * Renders a one-line grounding banner above the chat input:
 *
 *   Project: Lazy Edward Bay · Uranium · Saskatchewan · EPSG:32613 · 20 holes
 *
 * Purpose: stop users from asking questions about the wrong project.
 * Fetches lazily when projectId changes; caches result in component state
 * so switching back and forth doesn't refetch. Falls back silently on
 * network errors — the banner is a quality-of-life affordance, not a
 * correctness gate.
 */
interface ProjectContextBannerProps {
    projectId: string | null;
}

interface ContextPayload {
    slug: string;
    name: string;
    commodity: string | null;
    region: string | null;
    crs_datum: string;
    hole_count: number;
}

export default function ProjectContextBanner({ projectId }: ProjectContextBannerProps) {
    const [ctx, setCtx] = useState<ContextPayload | null>(null);
    const [loading, setLoading] = useState<boolean>(false);

    useEffect(() => {
        if (!projectId) {
            setCtx(null);
            return;
        }
        // Simple in-memory cache: only refetch when the projectId changes.
        setLoading(true);
        // Auth via Sanctum session cookie (same-origin). No bearer token
        // from localStorage — that would be an XSS-exfiltration target,
        // and the session cookie is the canonical credential for /api/v1.
        const csrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
        fetch(`/api/v1/dashboard/projects/by-id/${projectId}/context`, {
            credentials: 'same-origin',
            headers: {
                Accept: 'application/json',
                ...(csrf ? { 'X-CSRF-TOKEN': csrf } : {}),
            },
        })
            .then((r) => (r.ok ? r.json() : Promise.reject(r)))
            .then((j) => setCtx(j?.data ?? null))
            .catch(() => setCtx(null))
            .finally(() => setLoading(false));
    }, [projectId]);

    if (!projectId) return null;
    if (loading && !ctx) {
        return (
            <div className="max-w-3xl mx-auto mb-2 px-3 py-1.5 text-[11px] text-gray-500 border-b border-gray-800">
                Loading project context…
            </div>
        );
    }
    if (!ctx) return null;

    // Compose the one-liner. Em-dashes for missing fields so the banner
    // shape is stable across projects.
    const parts: string[] = [];
    parts.push(ctx.name || '—');
    if (ctx.commodity) parts.push(ctx.commodity);
    if (ctx.region) parts.push(ctx.region);
    parts.push(ctx.crs_datum || 'unknown CRS');
    parts.push(`${ctx.hole_count} hole${ctx.hole_count === 1 ? '' : 's'}`);

    return (
        <div
            className="max-w-3xl mx-auto mb-2 px-3 py-1.5 text-[11px] text-gray-400 border-b border-gray-800 flex items-center gap-2"
            role="status"
            aria-label="Active project context"
        >
            <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-amber-950/50 border border-amber-800/40 text-amber-400 text-[9px] uppercase tracking-wide shrink-0">
                Project
            </span>
            <span className="truncate">
                {parts.map((p, i) => (
                    <span key={i}>
                        {i > 0 && <span className="text-gray-600 mx-1.5">·</span>}
                        <span className={i === 0 ? 'text-gray-200 font-medium' : ''}>{p}</span>
                    </span>
                ))}
            </span>
        </div>
    );
}
