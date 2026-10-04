/**
 * Guards for what lands in the main / page chunks (2026-09-29 audit). The
 * real check is the build manifest; these keep the source from regressing.
 */
import { describe, expect, it } from 'vitest';
import bootstrapSource from '../../bootstrap.ts?raw';
import workspaceSource from '../../Pages/Foundry/Workspace.tsx?raw';
import packageJson from '../../../../package.json';

describe('bundle hygiene', () => {
    it('does not ship Axios (FE-23 — Inertia v3 dropped it; nothing called it)', () => {
        expect(bootstrapSource).not.toMatch(/from 'axios'/);
        expect(bootstrapSource).not.toContain('window.axios');
        const deps = { ...(packageJson.dependencies ?? {}), ...(packageJson.devDependencies ?? {}) } as Record<
            string,
            string
        >;
        expect(deps.axios).toBeUndefined();
    });

    it('Workspace loads every Plotly view lazily (FE-10)', () => {
        expect(workspaceSource).not.toMatch(/^import .*Borehole3DView/m);
        expect(workspaceSource).toContain("lazy(() => import('@/Components/Foundry/Borehole3DView'))");
    });
});
