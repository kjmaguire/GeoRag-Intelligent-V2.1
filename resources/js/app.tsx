import './bootstrap';
import '@fontsource/inter-tight/300.css';
import '@fontsource/inter-tight/400.css';
import '@fontsource/inter-tight/500.css';
import '@fontsource/inter-tight/600.css';
import '@fontsource/inter-tight/700.css';
import '@fontsource/jetbrains-mono/400.css';
import '@fontsource/jetbrains-mono/500.css';
import '@fontsource/jetbrains-mono/600.css';
import { createInertiaApp } from '@inertiajs/react';
import { createRoot } from 'react-dom/client';
import { ErrorBoundary } from './Components/ErrorBoundary';

/**
 * GeoRAG Intelligence — Inertia.js entry point.
 *
 * Layout pattern: each Page component is responsible for wrapping itself in
 * AppLayout. This keeps page-level concerns (project state, chat state)
 * colocated with the layout rather than hoisted into a global wrapper.
 *
 * Code splitting: pages are resolved via `import.meta.glob` WITHOUT
 * `eager: true` so each page ships as its own Vite chunk. The login page no
 * longer pulls in Plotly / MapLibre / React Flow / the 1350-line Chat.tsx —
 * they're only fetched once the user navigates to Chat or Explorer.
 *
 * Error boundary: the whole tree is wrapped in a root-scope ErrorBoundary so
 * an unexpected render throw in any page yields a recovery UI instead of a
 * blank screen.
 */
createInertiaApp({
    resolve: (name: string) => {
        const pages = import.meta.glob('./Pages/**/*.tsx') as Record<
            string,
            () => Promise<{ default: React.ComponentType }>
        >;
        const loader = pages[`./Pages/${name}.tsx`];
        if (!loader) {
            throw new Error(`[Inertia] page not found: ${name}`);
        }
        return loader();
    },
    setup({ el, App, props }) {
        createRoot(el!).render(
            <ErrorBoundary scope="root">
                <App {...props} />
            </ErrorBoundary>,
        );
    },
    progress: {
        color: '#f59e0b', // amber-500 — matches the brand bar
        showSpinner: false,
    },
});
