import { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react';
import { Link } from '@inertiajs/react';

/**
 * ToastHost — app-wide toast notifications for the Foundry shell.
 *
 * Before this existed the only toast in the app was MapView's local
 * tile-failure one; nothing shell-level could announce cross-page events
 * (e.g. "file finished ingesting" while the user is on Chat).
 *
 * Usage:
 *   - Mount <ToastProvider> once, inside the themed `.foundry` root
 *     (FoundryShell does this) so the viewport inherits the theme tokens.
 *   - Call `useToast().pushToast({ title, detail?, tone?, href?, linkLabel? })`
 *     from any descendant.
 *
 * Toasts stack bottom-right, auto-dismiss (default 8 s), and are
 * dismissible by hand. The viewport is `aria-live="polite"` so screen
 * readers announce arrivals without interrupting.
 */

export interface ToastInput {
    title: string;
    detail?: string;
    tone?: 'accent' | 'warn' | 'info' | 'neutral';
    /** Optional Inertia link rendered under the text. */
    href?: string;
    linkLabel?: string;
    /** Auto-dismiss delay. Defaults to 8000 ms. */
    durationMs?: number;
}

interface ToastRecord extends ToastInput {
    id: number;
}

interface ToastContextValue {
    pushToast: (toast: ToastInput) => void;
}

const ToastContext = createContext<ToastContextValue>({
    // No-op fallback so a component rendered outside the provider (tests,
    // storybook-style harnesses) degrades silently instead of crashing.
    pushToast: () => {},
});

export function useToast(): ToastContextValue {
    return useContext(ToastContext);
}

const DEFAULT_DURATION_MS = 8000;

function toneColor(tone: ToastInput['tone']): string {
    switch (tone) {
        case 'accent':
            return 'var(--accent)';
        case 'warn':
            return 'var(--warn)';
        case 'info':
            return 'var(--info)';
        default:
            return 'var(--fg-2)';
    }
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
    const [toasts, setToasts] = useState<ToastRecord[]>([]);
    const nextIdRef = useRef(1);
    const timersRef = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());

    const dismiss = useCallback((id: number) => {
        setToasts((prev) => prev.filter((t) => t.id !== id));
        const timer = timersRef.current.get(id);
        if (timer) {
            clearTimeout(timer);
            timersRef.current.delete(id);
        }
    }, []);

    const pushToast = useCallback(
        (toast: ToastInput) => {
            const id = nextIdRef.current++;
            setToasts((prev) => [...prev, { ...toast, id }]);
            const timer = setTimeout(() => dismiss(id), toast.durationMs ?? DEFAULT_DURATION_MS);
            timersRef.current.set(id, timer);
        },
        [dismiss],
    );

    // Memoised: `{ pushToast }` built inline is a new object on every
    // render, and this component re-renders on every toast push and every
    // auto-dismiss. That re-rendered every consumer of the context — which
    // is the whole Foundry shell — for a notification in the corner.
    const contextValue = useMemo(() => ({ pushToast }), [pushToast]);

    return (
        <ToastContext.Provider value={contextValue}>
            {children}
            {/* Viewport — fixed bottom-right, above content but below the
                command palette (z-200) so ⌘K stays on top. */}
            <div
                className="fixed bottom-4 right-4 z-[180] flex flex-col gap-2 items-end pointer-events-none"
                role="status"
                aria-live="polite"
            >
                {toasts.map((t) => (
                    <div
                        key={t.id}
                        className="pointer-events-auto w-80 max-w-[92vw] rounded-md border px-3 py-2.5"
                        style={{
                            background: 'var(--bg-1)',
                            borderColor: 'var(--line-2)',
                            boxShadow: '0 12px 32px rgba(0,0,0,0.4)',
                        }}
                    >
                        <div className="flex items-start gap-2">
                            <span
                                className="mt-1 inline-block h-1.5 w-1.5 rounded-full shrink-0"
                                style={{ background: toneColor(t.tone) }}
                            />
                            <div className="flex-1 min-w-0">
                                <div className="text-xs font-medium truncate" style={{ color: 'var(--fg-0)' }} title={t.title}>
                                    {t.title}
                                </div>
                                {t.detail && (
                                    <div className="text-[11px] mt-0.5" style={{ color: 'var(--fg-2)' }}>
                                        {t.detail}
                                    </div>
                                )}
                                {t.href && (
                                    <Link
                                        href={t.href}
                                        onClick={() => dismiss(t.id)}
                                        className="inline-block mt-1.5 text-[10px] font-mono uppercase tracking-wider underline"
                                        style={{ color: 'var(--accent)' }}
                                    >
                                        {t.linkLabel ?? 'View'} →
                                    </Link>
                                )}
                            </div>
                            <button
                                type="button"
                                onClick={() => dismiss(t.id)}
                                aria-label="Dismiss notification"
                                className="text-[11px] font-mono leading-none px-1 py-0.5 rounded"
                                style={{ color: 'var(--fg-3)' }}
                            >
                                ×
                            </button>
                        </div>
                    </div>
                ))}
            </div>
        </ToastContext.Provider>
    );
}
