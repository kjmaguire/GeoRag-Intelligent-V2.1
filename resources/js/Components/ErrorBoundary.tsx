import { Component, type ErrorInfo, type ReactNode } from 'react';

/**
 * Global React error boundary.
 *
 * Catches render / lifecycle / constructor errors in child components and
 * renders a minimal recovery UI instead of a blank page. Without this, any
 * uncaught throw in a page component (Chat.tsx, Explorer.tsx, Portfolio.tsx,
 * the MapLibre / Plotly / StripLog viewers) crashes the whole React tree and
 * leaves the user staring at an empty DOM.
 *
 * Does NOT catch:
 *   - Event handler errors (those need try/catch inside the handler)
 *   - Async errors (promise rejections in fetch/useEffect bodies)
 *   - SSR errors (this is client-only by design)
 *
 * Telemetry: errors are POSTed to /api/v1/client-errors best-effort. The
 * endpoint is optional — 404 is tolerated so local dev still works without
 * wiring the collector.
 */

interface Props {
    children: ReactNode;
    /** Optional label for telemetry so we can tell "root boundary" from "viz boundary". */
    scope?: string;
}

interface State {
    error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
    state: State = { error: null };

    static getDerivedStateFromError(error: Error): State {
        return { error };
    }

    componentDidCatch(error: Error, info: ErrorInfo): void {
        // Best-effort telemetry. Silent on failure.
        try {
            const payload = {
                scope: this.props.scope ?? 'root',
                message: error.message,
                stack: error.stack,
                componentStack: info.componentStack,
                url: typeof window !== 'undefined' ? window.location.href : null,
                userAgent: typeof navigator !== 'undefined' ? navigator.userAgent : null,
            };
            if (typeof fetch === 'function') {
                fetch('/api/v1/client-errors', {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                    keepalive: true,
                }).catch(() => {});
            }
        } catch {
            /* telemetry is best-effort */
        }
        // Always surface in console for developers.
        // eslint-disable-next-line no-console
        console.error('[ErrorBoundary]', this.props.scope ?? 'root', error, info.componentStack);
    }

    handleReset = (): void => {
        this.setState({ error: null });
    };

    handleReload = (): void => {
        if (typeof window !== 'undefined') {
            window.location.reload();
        }
    };

    render(): ReactNode {
        if (!this.state.error) return this.props.children;

        const isDev =
            typeof import.meta !== 'undefined' &&
            (import.meta as { env?: { DEV?: boolean } }).env?.DEV === true;

        return (
            <div
                role="alert"
                aria-live="assertive"
                className="min-h-screen bg-gray-950 text-gray-100 flex items-center justify-center px-4"
            >
                <div className="w-full max-w-md bg-gray-900 border border-red-900/50 rounded-xl p-6 shadow-xl">
                    <div className="flex items-center gap-3 mb-4">
                        <div className="w-2 h-8 bg-red-500 rounded-sm" />
                        <h1 className="text-lg font-semibold">Something went wrong</h1>
                    </div>
                    <p className="text-sm text-gray-400 mb-4">
                        The page hit an unexpected error. Your session is still active — try
                        recovering the current view or reloading the app.
                    </p>
                    {isDev && (
                        <pre className="text-xs text-red-300 bg-black/40 border border-red-900/40 rounded-lg p-3 mb-4 overflow-auto max-h-48 whitespace-pre-wrap break-all">
                            {this.state.error.message}
                            {this.state.error.stack ? '\n\n' + this.state.error.stack : ''}
                        </pre>
                    )}
                    <div className="flex items-center gap-2">
                        <button
                            type="button"
                            onClick={this.handleReset}
                            className="flex-1 bg-amber-600 hover:bg-amber-500 text-white font-medium rounded-lg py-2 text-sm transition-colors focus:outline-none focus:ring-2 focus:ring-amber-500 focus:ring-offset-2 focus:ring-offset-gray-900"
                        >
                            Try again
                        </button>
                        <button
                            type="button"
                            onClick={this.handleReload}
                            className="flex-1 bg-gray-800 hover:bg-gray-700 text-gray-200 font-medium rounded-lg py-2 text-sm transition-colors focus:outline-none focus:ring-2 focus:ring-gray-500 focus:ring-offset-2 focus:ring-offset-gray-900"
                        >
                            Reload
                        </button>
                    </div>
                </div>
            </div>
        );
    }
}

export default ErrorBoundary;
