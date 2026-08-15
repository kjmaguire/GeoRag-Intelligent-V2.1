import { Head, Link, usePage } from '@inertiajs/react';
import type { PageProps } from '@/types';

/**
 * Error - shared Inertia-rendered error page for HTTP 403/404/419/429/500/503.
 *
 * Bug fix (2026-08-15, live-browser-observed): before this page existed,
 * any 404/403/500 in the app (e.g. a stale citation "Open in Reader" link,
 * a deleted project, a mistyped URL) fell through to Laravel's bare
 * framework error page - plain "404 | Not Found" text, no header, no nav,
 * no way back into the app except the browser back button. Wired up in
 * bootstrap/app.php's withExceptions() so every non-debug error response
 * renders through the normal app shell instead.
 */

interface ErrorPageProps {
    status: number;
}

const COPY: Record<number, { title: string; body: string }> = {
    403: {
        title: 'Access denied',
        body: "You don't have permission to view this. If you think that's wrong, check you're in the right workspace or ask an admin for access.",
    },
    404: {
        title: 'Page not found',
        body: "This link doesn't point anywhere - the project, report, or resource may have been renamed, moved, or removed.",
    },
    419: {
        title: 'Session expired',
        body: 'Your session timed out for security. Sign in again to pick up where you left off.',
    },
    429: {
        title: 'Too many requests',
        body: "You've hit a rate limit. Wait a moment and try again.",
    },
    500: {
        title: 'Something went wrong',
        body: "That's on us, not you. The error's been logged - try again in a moment.",
    },
    503: {
        title: 'Down for maintenance',
        body: "GeoRAG is briefly unavailable while we do some work. Try again shortly.",
    },
};

export default function ErrorPage({ status }: ErrorPageProps) {
    const { auth } = usePage<PageProps>().props;
    const copy = COPY[status] ?? {
        title: 'Unexpected error',
        body: 'Something did not work as expected.',
    };
    const homeHref = auth?.user ? '/projects' : '/login';
    const homeLabel = auth?.user ? '<- Back to projects' : '<- Back to sign in';

    return (
        <div
            style={{
                minHeight: '100vh',
                background: 'var(--bg-0)',
                color: 'var(--fg-1)',
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 16,
                padding: 24,
                textAlign: 'center',
            }}
        >
            <Head title={`${status} - GeoRAG`} />
            <div
                style={{
                    fontFamily: 'var(--font-mono)',
                    fontSize: 12,
                    letterSpacing: '0.12em',
                    textTransform: 'uppercase',
                    color: 'var(--fg-3)',
                }}
            >
                GeoRAG - Error {status}
            </div>
            <h1 style={{ fontSize: 28, fontWeight: 600, color: 'var(--fg-0)', margin: 0 }}>
                {copy.title}
            </h1>
            <p style={{ maxWidth: 440, fontSize: 14, color: 'var(--fg-2)', margin: 0 }}>
                {copy.body}
            </p>
            <div style={{ display: 'flex', gap: 12, marginTop: 8 }}>
                <Link
                    href={homeHref}
                    style={{
                        fontFamily: 'var(--font-mono)',
                        fontSize: 11,
                        textTransform: 'uppercase',
                        letterSpacing: '0.08em',
                        padding: '8px 16px',
                        borderRadius: 6,
                        border: '1px solid var(--accent-dim)',
                        color: 'var(--accent)',
                        background: 'var(--accent-bg)',
                        textDecoration: 'none',
                    }}
                >
                    {homeLabel}
                </Link>
            </div>
        </div>
    );
}
