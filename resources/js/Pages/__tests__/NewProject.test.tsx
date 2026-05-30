/**
 * NewProject.test.tsx
 *
 * Security regression guard: NewProject must NOT read auth tokens from
 * localStorage. Its project-creation fetch uses Sanctum session cookie via
 * `credentials: 'same-origin'` (types.ts:11-12).
 *
 * The test renders the page (Step 1 form visible on mount) and verifies that
 * no localStorage getItem call with a token-like key has occurred.
 * The form submit path is not exercised here — that would require DOM event
 * simulation and a second fetch mock; the on-mount guard is the regression
 * boundary for the security migration.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render } from '@testing-library/react';

// Mock Inertia — NewProject uses Head and router.visit
vi.mock('@inertiajs/react', () => ({
    Head: ({ children }: any) => <>{children}</>,
    router: { visit: vi.fn() },
}));

// Mock AppLayout to avoid its own fetch (ProjectSelector) bleeding into this test
vi.mock('../../Layouts/AppLayout', () => ({
    default: ({ children }: any) => <div>{children}</div>,
}));

import NewProject from '../NewProject';

describe('NewProject — auth surface', () => {
    let getItemSpy: ReturnType<typeof vi.spyOn>;

    beforeEach(() => {
        getItemSpy = vi.spyOn(Storage.prototype, 'getItem');
        vi.spyOn(globalThis, 'fetch').mockResolvedValue(
            new Response(JSON.stringify({ data: { project_id: 'proj-123' } }), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            }),
        );
    });

    afterEach(() => {
        getItemSpy.mockRestore();
        vi.restoreAllMocks();
    });

    it('does not read auth tokens from localStorage on mount', () => {
        render(<NewProject />);

        const tokenLike = /token|jwt|secret/i;
        const offending = getItemSpy.mock.calls
            .map(([key]) => String(key))
            .filter((k) => tokenLike.test(k));
        expect(offending).toEqual([]);
    });
});
