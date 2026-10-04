import { describe, expect, it } from 'vitest';
import { LOG_PROPS, VIZ3D_PROPS, copilotQuickPrompts, crsLabel, initialView3D, reloadPlan } from '@/lib/workspacePage';

describe('reloadPlan (FE-11)', () => {
    it('reloads everything for collar or assay changes', () => {
        expect(reloadPlan(['collars', 'reports'])).toEqual({ props: 'all', viz3d: true });
        expect(reloadPlan(['assays'])).toEqual({ props: 'all', viz3d: true });
    });

    it('does nothing for types the page does not show', () => {
        expect(reloadPlan(['reports', 'quality', 'review_queue'])).toEqual({ props: [], viz3d: false });
    });

    it('scopes a structures change to map layers, extent and counts', () => {
        const plan = reloadPlan(['structures']);
        expect(plan.viz3d).toBe(true);
        expect(plan.props).toEqual(expect.arrayContaining(['project_layers', 'project_extent', 'structures_count']));
        expect(plan.props).not.toContain('collars');
        // The 3D group is fetched separately, when a 3D mode is on screen.
        for (const p of VIZ3D_PROPS) expect(plan.props).not.toContain(p);
    });

    it('scopes a curves change to the LOGS panel', () => {
        const plan = reloadPlan(['curves']);
        expect(plan.props).toEqual(expect.arrayContaining([...LOG_PROPS, 'curve_summary']));
    });
});

describe('initialView3D (FE-25)', () => {
    const none = { intervalsCount: 0, collarsCount: 0, structuresCount: 0, structuresVisualCount: 0 };

    it('prefers lithology, then trajectories', () => {
        expect(initialView3D({ ...none, intervalsCount: 3, collarsCount: 2 })).toBe('lithology');
        expect(initialView3D({ ...none, collarsCount: 2 })).toBe('trajectories');
    });

    it('opens Structure Discs, not an empty Stereosphere, when only gold structures exist', () => {
        expect(initialView3D({ ...none, structuresVisualCount: 5 })).toBe('structure_discs');
        expect(initialView3D({ ...none, structuresCount: 5 })).toBe('stereosphere');
    });
});

describe('crsLabel (FE-18)', () => {
    it('names the project CRS instead of UTM 13N', () => {
        expect(crsLabel(26912)).toBe('EPSG:26912');
        expect(crsLabel(null)).toBe('CRS not declared');
    });
});

describe('copilotQuickPrompts (FE-25)', () => {
    it('is commodity-neutral with no commodity', () => {
        const prompts = copilotQuickPrompts(null).join(' ');
        expect(prompts).not.toMatch(/U₃O₈|uranium|Smith Ranch/i);
    });

    it('uses the project commodity when there is one', () => {
        expect(copilotQuickPrompts('Gold')[1]).toBe('Which holes have the best Gold intervals?');
        expect(copilotQuickPrompts('Gold').join(' ')).not.toMatch(/U₃O₈|Smith Ranch/);
    });
});
