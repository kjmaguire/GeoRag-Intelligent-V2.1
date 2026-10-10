/**
 * FE-24 — the compare modal is a real dialog: named, modal, Escape closes it,
 * and focus moves inside it.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CompareHolesModal, CompareHolesPanel } from '../CompareHolesModal';

afterEach(() => {
    vi.restoreAllMocks();
});

describe('CompareHolesModal', () => {
    it('has dialog semantics, closes on Escape and takes focus', async () => {
        vi.spyOn(globalThis, 'fetch').mockReturnValue(new Promise(() => {}));
        const onClose = vi.fn();
        render(<CompareHolesModal projectSlug="p" leftHole="A-1" rightHole="B-2" onClose={onClose} />);

        const dialog = screen.getByRole('dialog', { name: 'Hole comparison: A-1 vs B-2' });
        await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));

        fireEvent.keyDown(dialog, { key: 'Escape' });
        expect(onClose).toHaveBeenCalledTimes(1);
    });
});

describe('CompareHolesPanel depth axis', () => {
    const hole = (id: string, totalDepth: number) => ({
        hole_id: id,
        collar_id: `c-${id}`,
        total_depth: totalDepth,
        easting: null,
        northing: null,
        lat: null,
        lng: null,
        // No curves, and the API's 600 m placeholder where it has none.
        log_tracks: [],
        log_depth_max: 600,
        lithology_intervals: [{ from: 0, to: totalDepth, code: id, label: id, color: '#8899aa' }],
        alteration_intervals: [],
        mineralization_intervals: [],
        ore_bands: 0,
        ore_thickness_m: 0,
        mean_u3o8_pct: null,
    });

    it('draws both holes on one axis that reaches the deeper hole, not on a fixed 600 m floor', async () => {
        vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
            const url = String(input);
            const id = url.includes('/holes/SHALLOW/') ? 'SHALLOW' : 'DEEP';
            return new Response(JSON.stringify(hole(id, id === 'SHALLOW' ? 130 : 400)), { status: 200 });
        });
        render(<CompareHolesPanel projectSlug="p" leftHole="SHALLOW" rightHole="DEEP" chartHeight={300} />);

        const shallow = await screen.findByLabelText('SHALLOW 0-130 m');
        const deep = await screen.findByLabelText('DEEP 0-400 m');
        // Axis = 400 m rounded up to 500 m; 300 px plot.
        expect(Number(shallow.getAttribute('height'))).toBeCloseTo((130 / 500) * 300, 5);
        expect(Number(deep.getAttribute('height'))).toBeCloseTo((400 / 500) * 300, 5);
        expect(shallow.getAttribute('y')).toBe(deep.getAttribute('y'));
    });
});
