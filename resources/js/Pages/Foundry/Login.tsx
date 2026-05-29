import { useState } from 'react';
import { Head, useForm } from '@inertiajs/react';

/**
 * Foundry Login — split-screen with atmospheric left panel and form right.
 *
 * Structural port of the Claude Design handoff `auth/LoginPage.jsx`:
 *  - 1.05fr / 1fr grid split
 *  - Left panel: radial-gradient atmosphere + SVG contour-line pattern +
 *    grid overlay + grain + brand mark at top + serif hero copy +
 *    italic-accent pull quote + bottom strat-column motif strip
 *  - Right panel: form with email + password (uppercase mono labels),
 *    submit button with spinner, divider, SSO + SAML buttons, isolation
 *    notice card, request-access link, footer with privacy/terms/status
 *
 * Narrative copy adapted from the prototype's Athabasca framing to the
 * user's real Wyoming Roll-Front Uranium (Cameco Shirley Basin) context.
 * Wired through the existing /api/v1/auth/spa-login Sanctum endpoint.
 */
export default function FoundryLogin() {
    const [stage] = useState<'password' | 'sso'>('password');
    const { data, setData, post, processing, errors } = useForm({
        email: '',
        password: '',
    });

    function submit(e: React.FormEvent) {
        e.preventDefault();
        if (!data.email || !data.password) return;
        post('/api/v1/auth/spa-login', { preserveScroll: true });
    }

    const loading = processing;

    return (
        <div
            className="foundry font-linear"
            style={{
                height: '100vh',
                background: 'var(--bg-0)',
                color: 'var(--fg-1)',
                display: 'grid',
                gridTemplateColumns: '1.05fr 1fr',
                overflow: 'hidden',
                position: 'relative',
            }}
        >
            <Head title="Sign in — GeoRAG" />

            {/* LEFT — atmosphere */}
            <div style={{ position: 'relative', overflow: 'hidden', borderRight: '1px solid var(--line-1)' }}>
                <div
                    style={{
                        position: 'absolute',
                        inset: 0,
                        background:
                            'radial-gradient(ellipse at 30% 20%, oklch(0.32 0.04 60 / 0.5) 0%, transparent 55%), radial-gradient(ellipse at 70% 80%, oklch(0.28 0.06 220 / 0.45) 0%, transparent 55%), var(--bg-0)',
                    }}
                />
                <svg width="100%" height="100%" preserveAspectRatio="xMidYMid slice" style={{ position: 'absolute', inset: 0, opacity: 0.18 }}>
                    <defs>
                        <pattern id="lg-contour" x="0" y="0" width="800" height="800" patternUnits="userSpaceOnUse">
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
                        <pattern id="lg-grid" x="0" y="0" width="48" height="48" patternUnits="userSpaceOnUse">
                            <path d="M 48 0 L 0 0 0 48" fill="none" stroke="oklch(0.78 0.06 200 / 0.3)" strokeWidth="0.5" />
                        </pattern>
                    </defs>
                    <rect width="100%" height="100%" fill="url(#lg-grid)" />
                    <rect width="100%" height="100%" fill="url(#lg-contour)" transform="translate(-100, -100)" />
                </svg>

                <div
                    style={{
                        position: 'absolute',
                        inset: 0,
                        backgroundImage: 'radial-gradient(rgba(255,255,255,0.025) 1px, transparent 1.5px)',
                        backgroundSize: '3px 3px',
                        mixBlendMode: 'overlay',
                    }}
                />

                {/* Brand mark */}
                <div style={{ position: 'absolute', top: 32, left: 32, display: 'flex', alignItems: 'center', gap: 10 }}>
                    <div
                        style={{
                            width: 28,
                            height: 28,
                            borderRadius: 4,
                            background: 'linear-gradient(135deg, var(--accent) 0%, oklch(0.55 0.16 220) 100%)',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            boxShadow: '0 0 24px var(--accent-dim)',
                        }}
                    >
                        <span style={{ fontFamily: 'var(--font-display)', fontSize: 16, color: '#0a0a0a', fontWeight: 600 }}>G</span>
                    </div>
                    <div>
                        <div style={{ fontFamily: 'var(--font-display)', fontSize: 17, color: 'var(--fg-0)', letterSpacing: '-0.005em' }}>
                            GeoRAG
                        </div>
                        <div style={{ fontSize: 9.5, color: 'var(--fg-3)', letterSpacing: '0.08em', textTransform: 'uppercase' }}>
                            Intelligence
                        </div>
                    </div>
                </div>

                {/* Hero copy — adapted to Wyoming context */}
                <div
                    style={{
                        position: 'relative',
                        height: '100%',
                        display: 'flex',
                        flexDirection: 'column',
                        justifyContent: 'center',
                        padding: '0 64px',
                        maxWidth: 640,
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
                        Roll-front uranium · Wyoming
                    </div>
                    <h1
                        style={{
                            fontFamily: 'var(--font-display)',
                            fontSize: 72,
                            color: 'var(--fg-0)',
                            letterSpacing: '-0.025em',
                            lineHeight: 0.95,
                            fontWeight: 400,
                        }}
                    >
                        The next ore body
                        <br />
                        is in your <em style={{ fontStyle: 'italic', color: 'var(--accent)' }}>archives</em>.
                    </h1>
                    <p style={{ fontSize: 15, color: 'var(--fg-2)', lineHeight: 1.55, marginTop: 22, maxWidth: 480 }}>
                        GeoRAG turns decades of drill logs, gamma traces, and WSGS scanned-paper archives into a queryable,
                        citation-grounded reasoning corpus — alongside live public geoscience.
                    </p>

                    <div style={{ marginTop: 40, paddingLeft: 16, borderLeft: '2px solid var(--accent)' }}>
                        <div
                            style={{
                                fontFamily: 'var(--font-display)',
                                fontSize: 17,
                                color: 'var(--fg-1)',
                                lineHeight: 1.4,
                                letterSpacing: '-0.005em',
                                fontStyle: 'italic',
                            }}
                        >
                            Phase B Tier 1 ingest brought 63 Shirley Basin collars + 753 well-log curves into silver
                            for the first time — gamma + grade indexed, every passage citation-anchored.
                        </div>
                        <div
                            style={{
                                fontSize: 11,
                                color: 'var(--fg-3)',
                                marginTop: 8,
                                letterSpacing: '0.04em',
                                textTransform: 'uppercase',
                            }}
                        >
                            Cluster 028N079W36 · Cameco 2011–2013 logs · WSGS public archive
                        </div>
                    </div>
                </div>

                {/* Bottom strat-column band */}
                <div style={{ position: 'absolute', bottom: 0, left: 0, right: 0, height: 6, display: 'flex' }}>
                    {['#3a3a32', '#d4a96a', '#5a4a3a', '#8a7a6a', '#1f1f1f', '#7cffd8', '#5a4a3a', '#d4a96a', '#a04545', '#8a7a6a', '#1f1f1f'].map(
                        (c, i, arr) => (
                            <div
                                key={i}
                                style={{
                                    flex: i === 5 ? 0.4 : (i + 3) % 4 === 0 ? 1.6 : 1,
                                    background: c,
                                    borderRight: i < arr.length - 1 ? '1px solid rgba(0,0,0,0.4)' : 'none',
                                }}
                            />
                        ),
                    )}
                </div>
            </div>

            {/* RIGHT — form */}
            <div
                style={{
                    display: 'flex',
                    flexDirection: 'column',
                    justifyContent: 'center',
                    padding: '0 72px',
                    background: 'var(--bg-0)',
                    position: 'relative',
                }}
            >
                <div style={{ width: '100%', maxWidth: 380, margin: '0 auto' }}>
                    <div
                        style={{
                            fontSize: 11,
                            color: 'var(--fg-3)',
                            letterSpacing: '0.06em',
                            textTransform: 'uppercase',
                        }}
                    >
                        Welcome back
                    </div>
                    <h2
                        style={{
                            fontFamily: 'var(--font-display)',
                            fontSize: 38,
                            color: 'var(--fg-0)',
                            letterSpacing: '-0.018em',
                            lineHeight: 1.05,
                            marginTop: 6,
                        }}
                    >
                        Sign in to GeoRAG
                    </h2>
                    <p style={{ fontSize: 12.5, color: 'var(--fg-3)', marginTop: 8, lineHeight: 1.5 }}>
                        Use your operator workspace credentials. SSO is recommended for team accounts.
                    </p>

                    <form onSubmit={submit} style={{ marginTop: 28, display: 'flex', flexDirection: 'column', gap: 14 }}>
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
                                type="email"
                                value={data.email}
                                onChange={(e) => setData('email', e.target.value)}
                                placeholder="you@operator.io"
                                autoFocus
                                autoComplete="email"
                                style={{
                                    width: '100%',
                                    padding: '10px 12px',
                                    fontSize: 13.5,
                                    color: 'var(--fg-0)',
                                    background: 'var(--bg-1)',
                                    border: '1px solid var(--line-1)',
                                    borderRadius: 5,
                                }}
                            />
                            {errors.email && (
                                <div style={{ fontSize: 10.5, color: 'var(--danger)', marginTop: 4 }}>{errors.email}</div>
                            )}
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
                                <a href="/forgot-password" style={{ fontSize: 10.5, color: 'var(--accent)' }}>
                                    Forgot?
                                </a>
                            </div>
                            <input
                                type="password"
                                value={data.password}
                                onChange={(e) => setData('password', e.target.value)}
                                placeholder="••••••••••"
                                autoComplete="current-password"
                                style={{
                                    width: '100%',
                                    padding: '10px 12px',
                                    fontSize: 13.5,
                                    color: 'var(--fg-0)',
                                    background: 'var(--bg-1)',
                                    border: '1px solid var(--line-1)',
                                    borderRadius: 5,
                                    fontFamily: 'ui-monospace, monospace',
                                }}
                            />
                            {errors.password && (
                                <div style={{ fontSize: 10.5, color: 'var(--danger)', marginTop: 4 }}>{errors.password}</div>
                            )}
                        </label>

                        <button
                            type="submit"
                            disabled={loading || !data.email || !data.password}
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
                                opacity: loading || !data.email || !data.password ? 0.6 : 1,
                                cursor: 'pointer',
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
                                    />{' '}
                                    Signing in…
                                </>
                            ) : (
                                <>
                                    Sign in →
                                </>
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
                            <span
                                style={{
                                    width: 14,
                                    height: 14,
                                    background: 'linear-gradient(135deg, #f25022 0 50%, #00a4ef 50% 100%)',
                                    display: 'inline-block',
                                }}
                            />
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
                            Indexes are isolated per workspace. Your private corpus is never co-mingled with other
                            operators or with public geoscience.
                        </div>
                    </div>

                    <div style={{ marginTop: 28, fontSize: 11, color: 'var(--fg-3)', textAlign: 'center' }}>
                        Need a workspace? <a href="mailto:hello@georag.io" style={{ color: 'var(--accent)' }}>Request access →</a>
                    </div>
                </div>

                <div
                    style={{
                        position: 'absolute',
                        bottom: 24,
                        left: 72,
                        right: 72,
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
                        <a href="#" style={{ color: 'var(--fg-3)' }}>Privacy</a>
                        <a href="#" style={{ color: 'var(--fg-3)' }}>Terms</a>
                        <a href="#" style={{ color: 'var(--fg-3)' }}>Status ●</a>
                    </span>
                </div>
            </div>

            <style>{`@keyframes lp-spin { to { transform: rotate(360deg); } }`}</style>
        </div>
    );
}

function LockIcon({ size = 11 }: { size?: number }) {
    return (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <rect x="5" y="11" width="14" height="10" rx="1" />
            <path d="M8 11V7a4 4 0 0 1 8 0v4" />
        </svg>
    );
}

function ShieldIcon({ size = 11, style }: { size?: number; style?: React.CSSProperties }) {
    return (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" style={style}>
            <path d="M12 3 L20 7 L20 12 C20 16 16 20 12 22 C8 20 4 16 4 12 L4 7 Z" />
        </svg>
    );
}
