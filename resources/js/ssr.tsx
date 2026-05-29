import { createInertiaApp } from '@inertiajs/react';
import createServer from '@inertiajs/react/server';
import { renderToString } from 'react-dom/server';

createServer((page) =>
    createInertiaApp({
        page,
        render: renderToString,
        resolve: (name: string) => {
            const pages = import.meta.glob('./Pages/**/*.tsx', { eager: true }) as Record<
                string,
                { default: React.ComponentType }
            >;
            return pages[`./Pages/${name}.tsx`];
        },
        setup: ({ App, props }) => <App {...props} />,
    }),
);
