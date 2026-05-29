import * as React from 'react';

/**
 * Foundry primitives — small UI building blocks used across every Foundry
 * page. Kept in one module so consumers can `import { Card, Kpi, Pill }
 * from '@/Components/Foundry/primitives'` and not chase a dozen files.
 *
 * Visual rules:
 *   - Use CSS variables from `.foundry` scope (defined in resources/css/app.css)
 *     directly via `var(--bg-1)` etc. — keeps the palette swappable.
 *   - Tailwind utilities for layout/spacing; OKLCH tokens for color.
 *   - No business logic — these are pure presentational components.
 */

type Tone = 'neutral' | 'accent' | 'warn' | 'danger' | 'info';

function toneVar(tone: Tone | undefined): string {
    switch (tone) {
        case 'accent':
            return 'var(--accent)';
        case 'warn':
            return 'var(--warn)';
        case 'danger':
            return 'var(--danger)';
        case 'info':
            return 'var(--info)';
        default:
            return 'var(--fg-1)';
    }
}

/* -------- Card -------- */

export function Card({
    children,
    title,
    eyebrow,
    actions,
    padded = true,
    className = '',
    contentClassName = '',
}: {
    children: React.ReactNode;
    title?: React.ReactNode;
    eyebrow?: React.ReactNode;
    actions?: React.ReactNode;
    padded?: boolean;
    className?: string;
    contentClassName?: string;
}) {
    return (
        <div
            className={['rounded-md border', className].join(' ')}
            style={{
                background: 'var(--bg-1)',
                borderColor: 'var(--line-1)',
            }}
        >
            {(title || eyebrow || actions) && (
                <div
                    className="flex items-center justify-between px-4 py-2.5 border-b"
                    style={{ borderColor: 'var(--line-1)' }}
                >
                    <div className="flex flex-col gap-0.5">
                        {eyebrow && (
                            <div className="text-[10px] font-mono uppercase tracking-[0.12em]" style={{ color: 'var(--fg-3)' }}>
                                {eyebrow}
                            </div>
                        )}
                        {title && (
                            <div className="text-sm font-medium" style={{ color: 'var(--fg-0)' }}>
                                {title}
                            </div>
                        )}
                    </div>
                    {actions && <div className="flex items-center gap-2">{actions}</div>}
                </div>
            )}
            <div className={[padded ? 'p-4' : '', contentClassName].join(' ').trim()}>{children}</div>
        </div>
    );
}

/* -------- Pill -------- */

export function Pill({
    children,
    tone = 'neutral',
    dot = false,
}: {
    children: React.ReactNode;
    tone?: Tone;
    dot?: boolean;
}) {
    const color = toneVar(tone);
    return (
        <span
            className="inline-flex items-center gap-1.5 px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider rounded border"
            style={{
                color,
                borderColor: color,
                background: tone === 'neutral' ? 'transparent' : 'color-mix(in oklch, ' + color + ' 12%, transparent)',
            }}
        >
            {dot && <span className="w-1.5 h-1.5 rounded-full" style={{ background: color }} />}
            {children}
        </span>
    );
}

/* -------- Stat (compact KPI cell) -------- */

export function Stat({
    label,
    value,
    sub,
    tone = 'neutral',
}: {
    label: string;
    value: React.ReactNode;
    sub?: React.ReactNode;
    tone?: Tone;
}) {
    return (
        <div className="px-4 py-3.5" style={{ background: 'var(--bg-1)' }}>
            <div className="text-[10px] font-mono uppercase tracking-[0.12em]" style={{ color: 'var(--fg-3)' }}>
                {label}
            </div>
            <div
                className="text-2xl font-semibold mt-1 leading-none"
                style={{
                    color: tone === 'accent' ? 'var(--accent)' : 'var(--fg-0)',
                    letterSpacing: '-0.015em',
                }}
            >
                {value}
            </div>
            {sub && (
                <div className="text-[10px] mt-1 font-mono" style={{ color: 'var(--fg-3)' }}>
                    {sub}
                </div>
            )}
        </div>
    );
}

/* -------- Segmented -------- */

export function Segmented<T extends string>({
    value,
    onChange,
    options,
}: {
    value: T;
    onChange: (v: T) => void;
    options: Array<{ value: T; label: React.ReactNode }>;
}) {
    return (
        <div
            className="inline-flex p-0.5 rounded border"
            style={{ background: 'var(--bg-2)', borderColor: 'var(--line-1)' }}
        >
            {options.map((opt) => (
                <button
                    key={opt.value}
                    type="button"
                    onClick={() => onChange(opt.value)}
                    className="px-2.5 py-1 text-[10px] font-mono uppercase tracking-wider rounded transition-colors"
                    style={{
                        background: value === opt.value ? 'var(--bg-3)' : 'transparent',
                        color: value === opt.value ? 'var(--fg-0)' : 'var(--fg-2)',
                    }}
                >
                    {opt.label}
                </button>
            ))}
        </div>
    );
}

/* -------- EmptyState -------- */

export function EmptyState({
    title,
    detail,
    action,
}: {
    title: React.ReactNode;
    detail?: React.ReactNode;
    action?: React.ReactNode;
}) {
    return (
        <div className="flex flex-col items-center justify-center py-16 px-6 text-center">
            <div className="w-10 h-10 rounded border border-dashed flex items-center justify-center mb-4" style={{ borderColor: 'var(--line-2)', color: 'var(--fg-3)' }}>
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                    <circle cx="12" cy="12" r="9" />
                    <path d="M8 12h8" />
                </svg>
            </div>
            <div className="text-sm font-medium" style={{ color: 'var(--fg-0)' }}>{title}</div>
            {detail && (
                <div className="text-xs mt-1.5 max-w-md" style={{ color: 'var(--fg-2)' }}>
                    {detail}
                </div>
            )}
            {action && <div className="mt-4">{action}</div>}
        </div>
    );
}

/* -------- Toolbar -------- */

export function Toolbar({ children }: { children: React.ReactNode }) {
    return (
        <div
            className="flex items-center gap-3 px-4 py-2 border-b"
            style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)' }}
        >
            {children}
        </div>
    );
}

/* -------- PageHeader -------- */

export function PageHeader({
    eyebrow,
    title,
    sub,
    actions,
}: {
    eyebrow?: React.ReactNode;
    title: React.ReactNode;
    sub?: React.ReactNode;
    actions?: React.ReactNode;
}) {
    return (
        <header
            className="flex items-end gap-4 px-8 pt-6 pb-4 border-b"
            style={{ borderColor: 'var(--line-1)' }}
        >
            <div className="flex-1">
                {eyebrow && (
                    <div className="text-[10px] font-mono uppercase tracking-[0.14em]" style={{ color: 'var(--fg-3)' }}>
                        {eyebrow}
                    </div>
                )}
                <h1
                    className="text-3xl font-semibold mt-1"
                    style={{ color: 'var(--fg-0)', letterSpacing: '-0.02em' }}
                >
                    {title}
                </h1>
                {sub && <div className="text-xs mt-1.5" style={{ color: 'var(--fg-3)' }}>{sub}</div>}
            </div>
            {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
    );
}

/* -------- Sparkline (SVG, no deps) -------- */

export function Sparkline({
    values,
    width = 80,
    height = 22,
    stroke = 'var(--accent)',
    fill = true,
}: {
    values: number[];
    width?: number;
    height?: number;
    stroke?: string;
    fill?: boolean;
}) {
    if (!values || values.length === 0) return <svg width={width} height={height} />;
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    const pts = values.map((v, i) => {
        const x = (i / Math.max(1, values.length - 1)) * width;
        const y = height - ((v - min) / range) * (height - 2) - 1;
        return [x, y] as const;
    });
    const d = pts.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
    const dFill = `${d} L${width},${height} L0,${height} Z`;
    return (
        <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden>
            {fill && <path d={dFill} fill={stroke} opacity="0.14" />}
            <path d={d} fill="none" stroke={stroke} strokeWidth="1.4" strokeLinejoin="round" strokeLinecap="round" />
        </svg>
    );
}

/* -------- ProgressBar -------- */

export function ProgressBar({
    value,
    max = 100,
    tone = 'accent',
    height = 4,
}: {
    value: number;
    max?: number;
    tone?: Tone;
    height?: number;
}) {
    const pct = Math.max(0, Math.min(100, (value / max) * 100));
    return (
        <div
            className="rounded-sm overflow-hidden w-full"
            style={{ background: 'var(--bg-3)', height }}
        >
            <div
                style={{
                    width: `${pct}%`,
                    height: '100%',
                    background: toneVar(tone),
                    transition: 'width 0.5s ease',
                }}
            />
        </div>
    );
}

/* -------- Modal (foundry-styled) -------- */

export function Modal({
    open,
    onClose,
    children,
    maxWidth = 920,
    label,
}: {
    open: boolean;
    onClose: () => void;
    children: React.ReactNode;
    maxWidth?: number;
    label?: string;
}) {
    if (!open) return null;
    return (
        <div
            className="fixed inset-0 z-[150] flex items-center justify-center p-4 foundry"
            style={{ background: 'rgba(8,10,14,0.78)', backdropFilter: 'blur(4px)' }}
            onClick={onClose}
            role="dialog"
            aria-modal="true"
            aria-label={label}
        >
            <div
                onClick={(e) => e.stopPropagation()}
                className="rounded-md border overflow-hidden flex flex-col max-h-[96vh]"
                style={{
                    background: 'var(--bg-0)',
                    borderColor: 'var(--line-2)',
                    boxShadow: '0 30px 90px rgba(0,0,0,0.8)',
                    width: `min(${maxWidth}px, 98vw)`,
                }}
            >
                {children}
            </div>
        </div>
    );
}

/* -------- Diamond brand mark -------- */

export function BrandDiamond({ size = 16 }: { size?: number }) {
    return (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
            <path d="M12 3 L21 8 L21 16 L12 21 L3 16 L3 8 Z" />
            <path d="M12 3 L12 21 M3 8 L21 16 M21 8 L3 16" opacity="0.35" />
        </svg>
    );
}

/* -------- StatusDot -------- */

export function StatusDot({ status }: { status: string | null }) {
    const map: Record<string, Tone> = {
        active: 'accent',
        ok: 'accent',
        complete: 'accent',
        synced: 'accent',
        indexing: 'info',
        syncing: 'info',
        degraded: 'warn',
        warn: 'warn',
        archived: 'neutral',
        paused: 'neutral',
        error: 'danger',
        refused: 'danger',
    };
    const tone = (status && map[status]) || 'neutral';
    return <span className="inline-block w-1.5 h-1.5 rounded-full" style={{ background: toneVar(tone) }} />;
}
