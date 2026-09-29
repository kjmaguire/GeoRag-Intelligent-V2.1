/**
 * FE-17 — horizontal planes (true_dip = 0) are real data, not missing data.
 */
import { render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

const plot = vi.hoisted(() => ({ data: [] as Array<Record<string, unknown>> }));
vi.mock('../../GeoPlot', () => ({
    default: ({ data }: { data: Array<Record<string, unknown>> }) => {
        plot.data = data;
        return null;
    },
}));

import Stereosphere, { isPlottableStructure } from '../Stereosphere';

describe('Stereosphere', () => {
    it('keeps a zero dip and drops only missing values', () => {
        expect(isPlottableStructure({ true_dip: 0, dip_direction: 0 })).toBe(true);
        expect(isPlottableStructure({ true_dip: null, dip_direction: 90 })).toBe(false);
        expect(isPlottableStructure({ true_dip: 30, dip_direction: null })).toBe(false);
    });

    it('plots flat bedding', () => {
        render(
            <Stereosphere
                holeId="h"
                structures={[{ depth: 10, structure_type: 'bedding', true_dip: 0, dip_direction: 0 }]}
            />,
        );
        const bedding = plot.data.find((t) => String(t.name ?? '').startsWith('bedding'));
        expect(bedding).toBeDefined();
    });
});
