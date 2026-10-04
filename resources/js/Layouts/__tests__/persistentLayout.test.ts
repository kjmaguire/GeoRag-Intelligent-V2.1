/**
 * FE-13 / FE-12 — the persistent default layout and the page globs.
 */
import { describe, expect, it, vi } from 'vitest';

vi.mock('@/Layouts/AppLayout', () => ({
    default: function AppLayoutStub() {
        return null;
    },
}));

import AppLayout from '@/Layouts/AppLayout';
import { SELF_WRAPPED_PAGES, resolvePageLayout } from '../persistentLayout';
import appEntry from '../../app.tsx?raw';
import ssrEntry from '../../ssr.tsx?raw';

const pageSources = import.meta.glob('../../Pages/**/*.tsx', {
    query: '?raw',
    import: 'default',
    eager: true,
}) as Record<string, string>;

function pageName(path: string): string {
    return path.replace('../../Pages/', '').replace(/\.tsx$/, '');
}

describe('resolvePageLayout', () => {
    it('gives Foundry pages the same (persistent) shell component', () => {
        expect(resolvePageLayout('Foundry/Workspace')).toBe(AppLayout);
        expect(resolvePageLayout('Foundry/Overview')).toBe(AppLayout);
    });

    it('gives auth / error pages and self-wrapped pages none', () => {
        expect(resolvePageLayout('Login')).toBeNull();
        expect(resolvePageLayout('Error')).toBeNull();
        for (const name of SELF_WRAPPED_PAGES) expect(resolvePageLayout(name)).toBeNull();
    });
});

describe('pages agree with the layout resolver', () => {
    const pages = Object.entries(pageSources).filter(([path]) => !path.includes('/__tests__/'));

    it('found the pages', () => {
        expect(pages.length).toBeGreaterThan(10);
    });

    it.each(pages.map(([path, src]) => [pageName(path), src] as const))(
        '%s wraps itself in AppLayout only when it gets no default layout',
        (name, src) => {
            const wrapsItself = src.includes('<AppLayout');
            if (resolvePageLayout(name) !== null) {
                // A second shell would nest inside the persistent one.
                expect(wrapsItself).toBe(false);
            } else if (SELF_WRAPPED_PAGES.has(name)) {
                expect(wrapsItself).toBe(true);
            }
        },
    );
});

describe('page globs exclude specs (FE-12)', () => {
    it.each([
        ['app.tsx', appEntry],
        ['ssr.tsx', ssrEntry],
    ])('%s negates Pages/**/__tests__', (_n, src) => {
        expect(src).toContain("'!./Pages/**/__tests__/**'");
    });
});
