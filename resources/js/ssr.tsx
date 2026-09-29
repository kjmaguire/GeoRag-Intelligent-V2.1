import { createInertiaApp } from '@inertiajs/react';
import createServer from '@inertiajs/react/server';
import { renderToString } from 'react-dom/server';
import { resolvePageLayout } from './Layouts/persistentLayout';

createServer((page) =>
    createInertiaApp({
        page,
        render: renderToString,
        layout: (name: string) => resolvePageLayout(name),
        resolve: (name: string) => {
            // Eager glob: without the negative pattern the SSR server would
            // import — and run — the page specs at boot (FE-12).
            const pages = import.meta.glob(['./Pages/**/*.tsx', '!./Pages/**/__tests__/**'], { eager: true }) as Record<
                string,
                { default: React.ComponentType }
            >;
            return pages[`./Pages/${name}.tsx`];
        },
        setup: ({ App, props }) => <App {...props} />,
    }),
);
