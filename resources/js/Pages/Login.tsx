import { useState, type JSX } from 'react';
import { Head, router, usePage } from '@inertiajs/react';
import type { PageProps } from '@/types';

/**
 * Login — split-screen Foundry auth (TrustGauge-style).
 *
 * Layout: 1.05fr left panel (atmosphere + hero copy + brand mark + strat
 * column motif) · 1fr right panel (form).
 *
 * Brand mark is the same hexagon SVG + GEORAG wordmark used in the
 * dashboard topbar (resources/js/Layouts/FoundryShell.tsx).
 *
 * Auth wiring unchanged: Sanctum CSRF cookie → /api/v1/auth/spa-login →
 * router.visit(return_to ?? '/chat').
 */

interface LoginApiResponse {
    token?: string;
    user: unknown;
    message?: string;
}

export default function Login(): JSX.Element {
    const { app } = usePage<PageProps>().props;
    const isDemoEnv = app?.env === 'local' || app?.env === 'development';
    const [email, setEmail] = useState<string>(isDemoEnv ? 'demo@georag.dev' : '');
    const [password, setPassword] = useState<string>('');
    const [error, setError] = useState<string | null>(null);
    const [loading, setLoading] = useState<boolean>(false);

    async function handleSubmit(e: React.FormEvent<HTMLFormElement>): Promise<void> {
        e.preventDefault();
        setError(null);
        setLoading(true);

        try {
            await fetch('/sanctum/csrf-cookie', { credentials: 'same-origin' });

            const xsrfToken = document.cookie
                .split('; ')
                .find((row) => row.startsWith('XSRF-TOKEN='))
                ?.split('=')[1];

            const res = await fetch('/api/v1/auth/spa-login', {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    Accept: 'application/json',
                    ...(xsrfToken ? { 'X-XSRF-TOKEN': decodeURIComponent(xsrfToken) } : {}),
                },
                body: JSON.stringify({ email, password }),
            });

            const data: LoginApiResponse = await res.json();

            if (!res.ok) {
                setError(data.message ?? 'Login failed');
                setLoading(false);
                return;
            }

            localStorage.setItem('georag_user', JSON.stringify(data.user));

            // Honour ?return_to=... from bootstrap.ts 401 handler. Same-site
            // absolute paths only — guard against open-redirect.
            let target = '/chat';
            try {
                const params = new URLSearchParams(window.location.search);
                const returnTo = params.get('return_to');
                if (
                    returnTo &&
                    returnTo.startsWith('/') &&
                    !returnTo.startsWith('//') &&
                    !returnTo.startsWith('/login')
                ) {
                    target = returnTo;
                }
            } catch {
                /* malformed query string is fine, keep default */
            }
            router.visit(target);
        } catch (err) {
            setError(err instanceof Error ? err.message : 'Network error');
            setLoading(false);
        }
    }

    return (
        <div
            className="foundry"
            style={{
                minHeight: '100vh',
                background: 'var(--bg-0)',
                color: 'var(--fg-1)',
                display: 'grid',
                gridTemplateColumns: 'minmax(0, 1.05fr) minmax(0, 1fr)',
                overflow: 'hidden',
                position: 'relative',
            }}
        >
            <Head title="Sign in — GeoRAG" />

            {/* ── LEFT — atmosphere + hero ─────────────────────────────── */}
            <div
                style={{
                    position: 'relative',
                    overflow: 'hidden',
                    borderRight: '1px solid var(--line-1)',
                    display: 'none',
                }}
                className="lp-left"
            >
                {/* radial wash */}
                <div
                    aria-hidden
                    style={{
                        position: 'absolute',
                        inset: 0,
                        background:
                            'radial-gradient(ellipse at 30% 20%, oklch(0.32 0.04 60 / 0.55) 0%, transparent 55%), radial-gradient(ellipse at 70% 80%, oklch(0.28 0.06 220 / 0.5) 0%, transparent 55%), var(--bg-0)',
                    }}
                />
                {/* contour + grid pattern */}
                <svg
                    aria-hidden
                    width="100%"
                    height="100%"
                    preserveAspectRatio="xMidYMid slice"
                    style={{ position: 'absolute', inset: 0, opacity: 0.18 }}
                >
                    <defs>
                        <pattern id="lp-contour" x="0" y="0" width="800" height="800" patternUnits="userSpaceOnUse">
                            {Array.from({ length: 12 }).map((_, i) => {
                                const r = 60 + i * 55;
                                return (
                                    <ellipse
                                        key={i}
                                        cx="400"
                                        cy="400"
                                        rx={r}
                                        ry={r * 0.62}
                                        fill="none"
                                        stroke="oklch(0.78 0.06 60)"
                                        strokeWidth="0.6"
                                    />
                                );
                            })}
                        </pattern>
                        <pattern id="lp-grid" x="0" y="0" width="48" height="48" patternUnits="userSpaceOnUse">
                            <path d="M 48 0 L 0 0 0 48" fill="none" stroke="oklch(0.78 0.06 200 / 0.3)" strokeWidth="0.5" />
                        </pattern>
                    </defs>
                    <rect width="100%" height="100%" fill="url(#lp-grid)" />
                    <rect width="100%" height="100%" fill="url(#lp-contour)" transform="translate(-100, -100)" />
                </svg>

                {/* grain overlay */}
                <div
                    aria-hidden
                    style={{
                        position: 'absolute',
                        inset: 0,
                        backgroundImage: 'radial-gradient(rgba(255,255,255,0.025) 1px, transparent 1.5px)',
                        backgroundSize: '3px 3px',
                        mixBlendMode: 'overlay',
                    }}
                />

                {/* Brand mark (dashboard logo) */}
                <div
                    style={{
                        position: 'absolute',
                        top: 32,
                        left: 40,
                        display: 'flex',
                        alignItems: 'center',
                        gap: 10,
                        zIndex: 2,
                        userSelect: 'none',
                    }}
                >
                    <GeoRAGMark size={22} />
                    <span
                        style={{
                            fontFamily: 'var(--font-mono)',
                            fontSize: 12,
                            fontWeight: 600,
                            letterSpacing: '0.14em',
                            color: 'var(--fg-0)',
                        }}
                    >
                        GEORAG
                    </span>
                </div>

                {/* Hero copy */}
                <div
                    style={{
                        position: 'relative',
                        height: '100%',
                        display: 'flex',
                        flexDirection: 'column',
                        justifyContent: 'center',
                        padding: '0 64px',
                        maxWidth: 680,
                        zIndex: 2,
                    }}
                >
                    <div
                        style={{
                            fontSize: 11,
                            color: 'var(--accent)',
                            letterSpacing: '0.1em',
                            textTransform: 'uppercase',
                            fontWeight: 600,
                            marginBottom: 14,
                        }}
                    >
                        Geological Intelligence Platform
                    </div>
                    <h1
                        style={{
                            fontFamily: 'var(--font-display)',
                            fontSize: 64,
                            color: 'var(--fg-0)',
                            letterSpacing: '-0.025em',
                            lineHeight: 0.98,
                            fontWeight: 500,
                            margin: 0,
                        }}
                    >
                        The next ore body
                        <br />
                        is in your{' '}
                        <em style={{ fontStyle: 'italic', color: 'var(--accent)', fontWeight: 400 }}>
                            archives
                        </em>
                        .
                    </h1>
                    <p
                        style={{
                            fontSize: 15,
                            color: 'var(--fg-2)',
                            lineHeight: 1.55,
                            marginTop: 22,
                            maxWidth: 480,
                        }}
                    >
                        GeoRAG turns decades of drill logs, gamma traces, and scanned-paper archives
                        into a queryable, citation-grounded reasoning corpus — alongside live public
                        geoscience.
                    </p>

                    <div
                        style={{
                            marginTop: 36,
                            paddingLeft: 16,
                            borderLeft: '2px solid var(--accent)',
                            maxWidth: 520,
                        }}
                    >
                        <div
                            style={{
                                fontFamily: 'var(--font-display)',
                                fontSize: 16,
                                color: 'var(--fg-1)',
                                lineHeight: 1.45,
                                letterSpacing: '-0.005em',
                                fontStyle: 'italic',
                            }}
                        >
                            Every answer cites the chunk it came from — drill collar, NI 43-101
                            paragraph, or assay row. No best-effort citations. No hallucinations.
                        </div>
                        <div
                            style={{
                                fontSize: 11,
                                color: 'var(--fg-3)',
                                marginTop: 10,
                                letterSpacing: '0.04em',
                                textTransform: 'uppercase',
                            }}
                        >
                            6-layer hallucination prevention · Workspace-isolated indexes
                        </div>
                    </div>
                </div>

                {/* Bottom strat-column motif */}
                <div
                    aria-hidden
                    style={{
                        position: 'absolute',
                        bottom: 0,
                        left: 0,
                        right: 0,
                        height: 6,
                        display: 'flex',
                    }}
                >
                    {[
                        '#3a3a32',
                        '#d4a96a',
                        '#5a4a3a',
                        '#8a7a6a',
                        '#1f1f1f',
                        '#7cffd8',
                        '#5a4a3a',
                        '#d4a96a',
                        '#a04545',
                        '#8a7a6a',
                        '#1f1f1f',
                    ].map((c, i, arr) => (
                        <div
                            key={i}
                            style={{
                                flex: i === 5 ? 0.4 : (i + 3) % 4 === 0 ? 1.6 : 1,
                                background: c,
                                borderRight: i < arr.length - 1 ? '1px solid rgba(0,0,0,0.4)' : 'none',
                            }}
                        />
                    ))}
                </div>
            </div>

            {/* ── RIGHT — form ─────────────────────────────────────────── */}
            <div
                style={{
                    display: 'flex',
                    flexDirection: 'column',
                    justifyContent: 'center',
                    padding: '48px 24px',
                    background: 'var(--bg-0)',
                    position: 'relative',
                    minHeight: '100vh',
                }}
            >
                {/* Mobile-only brand (when left panel hidden) */}
                <div
                    className="lp-mobile-brand"
                    style={{
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        gap: 10,
                        marginBottom: 24,
                        userSelect: 'none',
                    }}
                >
                    <GeoRAGMark size={20} />
                    <span
                        style={{
                            fontFamily: 'var(--font-mono)',
                            fontSize: 12,
                            fontWeight: 600,
                            letterSpacing: '0.14em',
                            color: 'var(--fg-0)',
                        }}
                    >
                        GEORAG
                    </span>
                </div>

                <div style={{ width: '100%', maxWidth: 380, margin: '0 auto' }}>
                    <div
                        style={{
                            fontSize: 11,
                            color: 'var(--fg-3)',
                            letterSpacing: '0.06em',
                            textTransform: 'uppercase',
                            fontWeight: 600,
                        }}
                    >
                        Welcome back
                    </div>
                    <h2
                        style={{
                            fontFamily: 'var(--font-display)',
                            fontSize: 36,
                            color: 'var(--fg-0)',
                            letterSpacing: '-0.018em',
                            lineHeight: 1.05,
                            marginTop: 6,
                            fontWeight: 500,
                        }}
                    >
                        Sign in to GeoRAG
                    </h2>
                    <p style={{ fontSize: 12.5, color: 'var(--fg-3)', marginTop: 8, lineHeight: 1.5 }}>
                        Use your operator workspace credentials. SSO is recommended for team accounts.
                    </p>

                    <form
                        onSubmit={handleSubmit}
                        style={{ marginTop: 28, display: 'flex', flexDirection: 'column', gap: 14 }}
                    >
                        {error && (
                            <div
                                role="alert"
                                style={{
                                    fontSize: 12,
                                    color: 'var(--danger)',
                                    background: 'oklch(0.28 0.08 25 / 0.25)',
                                    border: '1px solid oklch(0.42 0.12 25 / 0.6)',
                                    borderRadius: 5,
                                    padding: '8px 12px',
                                }}
                            >
                                {error}
                            </div>
                        )}

                        <label style={{ display: 'block' }}>
                            <div
                                style={{
                                    fontSize: 10.5,
                                    color: 'var(--fg-3)',
                                    letterSpacing: '0.04em',
                                    textTransform: 'uppercase',
                                    marginBottom: 6,
                                }}
                            >
                                Work email
                            </div>
                            <input
                                id="email"
                                type="email"
                                value={email}
                                onChange={(e) => setEmail(e.target.value)}
                                required
                                autoFocus
                                autoComplete="email"
                                placeholder="you@operator.io"
                                style={{
                                    width: '100%',
                                    padding: '10px 12px',
                                    fontSize: 13.5,
                                    color: 'var(--fg-0)',
                                    background: 'var(--bg-1)',
                                    border: '1px solid var(--line-1)',
                                    borderRadius: 5,
                                    outline: 'none',
                                }}
                            />
                        </label>

                        <label style={{ display: 'block' }}>
                            <div style={{ display: 'flex', alignItems: 'center', marginBottom: 6 }}>
                                <span
                                    style={{
                                        fontSize: 10.5,
                                        color: 'var(--fg-3)',
                                        letterSpacing: '0.04em',
                                        textTransform: 'uppercase',
                                    }}
                                >
                                    Password
                                </span>
                                <span style={{ flex: 1 }} />
                                <a
                                    href="/forgot-password"
                                    style={{
                                        fontSize: 10.5,
                                        color: 'var(--accent)',
                                        textDecoration: 'none',
                                    }}
                                >
                                    Forgot?
                                </a>
                            </div>
                            <input
                                id="password"
                                type="password"
                                value={password}
                                onChange={(e) => setPassword(e.target.value)}
                                required
                                autoComplete="current-password"
                                placeholder="••••••••••"
                                style={{
                                    width: '100%',
                                    padding: '10px 12px',
                                    fontSize: 13.5,
                                    color: 'var(--fg-0)',
                                    background: 'var(--bg-1)',
                                    border: '1px solid var(--line-1)',
                                    borderRadius: 5,
                                    outline: 'none',
                                    fontFamily: 'var(--font-mono)',
                                }}
                            />
                        </label>

                        <button
                            type="submit"
                            disabled={loading || !email || !password}
                            style={{
                                marginTop: 6,
                                padding: '11px 16px',
                                fontSize: 13,
                                color: 'var(--accent)',
                                background: 'var(--accent-bg)',
                                border: '1px solid var(--accent-dim)',
                                borderRadius: 5,
                                fontWeight: 500,
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'center',
                                gap: 8,
                                opacity: loading || !email || !password ? 0.6 : 1,
                                cursor: loading || !email || !password ? 'not-allowed' : 'pointer',
                                transition: 'opacity 120ms ease',
                            }}
                        >
                            {loading ? (
                                <>
                                    <span
                                        style={{
                                            width: 12,
                                            height: 12,
                                            border: '1.5px solid var(--accent)',
                                            borderTopColor: 'transparent',
                                            borderRadius: '50%',
                                            animation: 'lp-spin 0.8s linear infinite',
                                        }}
                                    />
                                    Signing in…
                                </>
                            ) : (
                                <>Sign in →</>
                            )}
                        </button>
                    </form>

                    <div style={{ display: 'flex', alignItems: 'center', gap: 10, margin: '22px 0' }}>
                        <div style={{ flex: 1, height: 1, background: 'var(--line-1)' }} />
                        <span
                            style={{
                                fontSize: 10,
                                color: 'var(--fg-3)',
                                letterSpacing: '0.06em',
                                textTransform: 'uppercase',
                            }}
                        >
                            or continue with
                        </span>
                        <div style={{ flex: 1, height: 1, background: 'var(--line-1)' }} />
                    </div>

                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
                        <button
                            type="button"
                            style={{
                                padding: '10px 12px',
                                fontSize: 12,
                                color: 'var(--fg-1)',
                                background: 'var(--bg-1)',
                                border: '1px solid var(--line-1)',
                                borderRadius: 5,
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'center',
                                gap: 8,
                                cursor: 'pointer',
                            }}
                        >
                            <MicrosoftLogo size={13} />
                            Microsoft SSO
                        </button>
                        <button
                            type="button"
                            style={{
                                padding: '10px 12px',
                                fontSize: 12,
                                color: 'var(--fg-1)',
                                background: 'var(--bg-1)',
                                border: '1px solid var(--line-1)',
                                borderRadius: 5,
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'center',
                                gap: 8,
                                cursor: 'pointer',
                            }}
                        >
                            <LockIcon size={11} /> SAML
                        </button>
                    </div>

                    {isDemoEnv && (
                        <div
                            style={{
                                marginTop: 22,
                                padding: '10px 12px',
                                border: '1px dashed var(--line-1)',
                                borderRadius: 5,
                                background: 'var(--bg-1)',
                                fontSize: 11,
                                color: 'var(--fg-3)',
                                textAlign: 'center',
                                fontFamily: 'var(--font-mono)',
                            }}
                        >
                            Demo:&nbsp;
                            <span style={{ color: 'var(--fg-1)' }}>demo@georag.dev</span>
                            &nbsp;/&nbsp;
                            <span style={{ color: 'var(--fg-1)' }}>georag2026</span>
                        </div>
                    )}

                    <div
                        style={{
                            marginTop: 24,
                            padding: '12px 14px',
                            border: '1px solid var(--line-1)',
                            borderRadius: 5,
                            background: 'var(--bg-1)',
                            display: 'flex',
                            alignItems: 'flex-start',
                            gap: 10,
                        }}
                    >
                        <ShieldIcon size={11} style={{ color: 'var(--fg-2)', marginTop: 2 }} />
                        <div style={{ fontSize: 11, color: 'var(--fg-3)', lineHeight: 1.5 }}>
                            Indexes are isolated per workspace. Your private corpus is never
                            co-mingled with other operators or with public geoscience.
                        </div>
                    </div>

                    <div
                        style={{
                            marginTop: 24,
                            fontSize: 11,
                            color: 'var(--fg-3)',
                            textAlign: 'center',
                        }}
                    >
                        Need a workspace?{' '}
                        <a
                            href="mailto:hello@georag.io"
                            style={{ color: 'var(--accent)', textDecoration: 'none' }}
                        >
                            Request access →
                        </a>
                    </div>
                </div>

                {/* Footer */}
                <div
                    style={{
                        position: 'absolute',
                        bottom: 20,
                        left: 24,
                        right: 24,
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        fontSize: 10,
                        color: 'var(--fg-3)',
                        letterSpacing: '0.04em',
                    }}
                >
                    <span>© 2026 GeoRAG Intelligence</span>
                    <span style={{ display: 'flex', gap: 14 }}>
                        <a href="#" style={{ color: 'var(--fg-3)', textDecoration: 'none' }}>
                            Privacy
                        </a>
                        <a href="#" style={{ color: 'var(--fg-3)', textDecoration: 'none' }}>
                            Terms
                        </a>
                        <a href="#" style={{ color: 'var(--fg-3)', textDecoration: 'none' }}>
                            Status ●
                        </a>
                    </span>
                </div>
            </div>

            <style>{`
                @keyframes lp-spin { to { transform: rotate(360deg); } }
                @media (min-width: 900px) {
                    .lp-left { display: block !important; }
                    .lp-mobile-brand { display: none !important; }
                }
            `}</style>
        </div>
    );
}

/** Hexagon + internal radial lines — matches FoundryShell topbar brand mark. */
function GeoRAGMark({ size = 16 }: { size?: number }) {
    return (
        <svg
            width={size}
            height={size}
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            style={{ color: 'var(--accent)' }}
            aria-hidden
        >
            <path d="M12 3 L21 8 L21 16 L12 21 L3 16 L3 8 Z" />
            <path d="M12 3 L12 21 M3 8 L21 16 M21 8 L3 16" opacity="0.35" />
        </svg>
    );
}

/** Microsoft 4-square logo (official quadrant colours). */
function MicrosoftLogo({ size = 13 }: { size?: number }) {
    return (
        <svg width={size} height={size} viewBox="0 0 23 23" aria-hidden xmlns="http://www.w3.org/2000/svg">
            <rect x="1" y="1" width="10" height="10" fill="#f25022" />
            <rect x="12" y="1" width="10" height="10" fill="#7fba00" />
            <rect x="1" y="12" width="10" height="10" fill="#00a4ef" />
            <rect x="12" y="12" width="10" height="10" fill="#ffb900" />
        </svg>
    );
}

function LockIcon({ size = 11 }: { size?: number }) {
    return (
        <svg
            width={size}
            height={size}
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden
        >
            <rect x="5" y="11" width="14" height="10" rx="1" />
            <path d="M8 11V7a4 4 0 0 1 8 0v4" />
        </svg>
    );
}

function ShieldIcon({
    size = 11,
    style,
}: {
    size?: number;
    style?: React.CSSProperties;
}) {
    return (
        <svg
            width={size}
            height={size}
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            style={style}
            aria-hidden
        >
            <path d="M12 3 L20 7 L20 12 C20 16 16 20 12 22 C8 20 4 16 4 12 L4 7 Z" />
        </svg>
    );
}
