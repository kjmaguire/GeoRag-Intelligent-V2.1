// @ts-nocheck
/**
 * StereonetCard.test.tsx
 *
 * Coverage (ADR-0007 PR-2):
 *   - Renders with 3 points including one with null `dip_direction_deg`
 *   - Renders the empty-state message when `points: []`
 *   - Header chips show projection + count
 *   - Click on a point fires `onPointClick` with the correct `source_row_id`
 *   - Tooltip text follows the strike/dip vs plunge/trend fallback rules
 *   - Point coordinates are mapped from unit-circle → CSS % with Y flipped
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import StereonetCard, { type StereonetMeta, type StereonetPoint } from '../StereonetCard';

// Tiny 1x1 transparent PNG (base64). Enough to satisfy the <img src>
// without going to the network. We never decode it in jsdom.
const TINY_PNG_B64 =
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=';

const POINTS: StereonetPoint[] = [
    {
        depth: 125.4,
        structure_type: 'foliation',
        strike_deg: 45,
        dip_deg: 60,
        dip_direction_deg: 135,
        plunge_deg: null,
        trend_deg: null,
        stereonet_x: 0.5,
        stereonet_y: 0.5,
        source_row_id: '11111111-1111-1111-1111-111111111111',
    },
    {
        // Null dip_direction_deg — must NOT render the " → ° " suffix
        depth: 87.0,
        structure_type: 'joint',
        strike_deg: 180,
        dip_deg: 30,
        dip_direction_deg: null,
        plunge_deg: null,
        trend_deg: null,
        stereonet_x: -0.4,
        stereonet_y: 0.2,
        source_row_id: '22222222-2222-2222-2222-222222222222',
    },
    {
        // Plunge/trend fallback (no strike/dip)
        depth: null,
        structure_type: 'lineation',
        strike_deg: null,
        dip_deg: null,
        dip_direction_deg: null,
        plunge_deg: 22,
        trend_deg: 310,
        stereonet_x: 0,
        stereonet_y: 0,
        source_row_id: '33333333-3333-3333-3333-333333333333',
    },
];

const META: StereonetMeta = {
    image_base64: TINY_PNG_B64,
    projection: 'Schmidt',
    structure_count: 3,
    points: POINTS,
};

describe('StereonetCard — empty state', () => {
    it('renders the ADR-0007 empty-state message when points: []', () => {
        render(
            <StereonetCard
                meta={{
                    image_base64: TINY_PNG_B64,
                    projection: 'Schmidt',
                    structure_count: 0,
                    points: [],
                }}
            />,
        );

        const empty = screen.getByTestId('stereonet-empty');
        expect(empty).toBeDefined();
        expect(empty.textContent ?? '').toContain('No structural measurements extracted');
        expect(empty.textContent ?? '').toContain('ADR-0007 PR-2');
    });

    it('still shows header chips in the empty state', () => {
        render(
            <StereonetCard
                meta={{
                    image_base64: '',
                    projection: 'Schmidt',
                    structure_count: 0,
                    points: [],
                }}
            />,
        );
        const chips = screen.getByTestId('stereonet-chips');
        expect(chips.textContent ?? '').toContain('Schmidt');
        expect(chips.textContent ?? '').toContain('0 pts');
    });
});

describe('StereonetCard — populated render', () => {
    it('renders header chips with projection + point count', () => {
        render(<StereonetCard meta={META} />);
        const chips = screen.getByTestId('stereonet-chips');
        expect(chips.textContent ?? '').toContain('Schmidt');
        expect(chips.textContent ?? '').toContain('3 pts');
    });

    it('renders the server-rendered PNG with the data:image/png;base64 prefix', () => {
        render(<StereonetCard meta={META} />);
        const img = screen.getByTestId('stereonet-png') as HTMLImageElement;
        expect(img.getAttribute('src') ?? '').toBe(`data:image/png;base64,${TINY_PNG_B64}`);
    });

    it('renders one overlay dot per in-bounds point', () => {
        render(<StereonetCard meta={META} />);
        expect(screen.getByTestId('stereonet-point-0')).toBeDefined();
        expect(screen.getByTestId('stereonet-point-1')).toBeDefined();
        expect(screen.getByTestId('stereonet-point-2')).toBeDefined();
    });

    it('maps unit-circle coordinates to CSS % with Y flipped', () => {
        render(<StereonetCard meta={META} />);
        // Point 0: x=0.5, y=0.5  → left 75%, top 25%
        const p0 = screen.getByTestId('stereonet-point-0') as HTMLButtonElement;
        expect(p0.style.left).toBe('75%');
        expect(p0.style.top).toBe('25%');

        // Point 2: x=0, y=0      → left 50%, top 50%
        const p2 = screen.getByTestId('stereonet-point-2') as HTMLButtonElement;
        expect(p2.style.left).toBe('50%');
        expect(p2.style.top).toBe('50%');
    });

    it('renders strike/dip tooltip and omits "→ °" when dip_direction_deg is null', () => {
        render(<StereonetCard meta={META} />);
        const p0 = screen.getByTestId('stereonet-point-0');
        const p1 = screen.getByTestId('stereonet-point-1');

        const t0 = p0.getAttribute('title') ?? '';
        const t1 = p1.getAttribute('title') ?? '';

        // Point 0 — has dip_direction
        expect(t0).toContain('foliation');
        expect(t0).toContain('strike 45° / dip 60°');
        expect(t0).toContain('→ 135°');
        expect(t0).toContain('125.4m');
        expect(t0).toContain('11111111');

        // Point 1 — null dip_direction, must NOT carry the arrow segment
        expect(t1).toContain('joint');
        expect(t1).toContain('strike 180° / dip 30°');
        expect(t1).not.toContain('→');
        expect(t1).toContain('87.0m');
        expect(t1).toContain('22222222');
    });

    it('falls back to plunge/trend tooltip when strike/dip are null', () => {
        render(<StereonetCard meta={META} />);
        const t2 = screen.getByTestId('stereonet-point-2').getAttribute('title') ?? '';
        expect(t2).toContain('lineation');
        expect(t2).toContain('plunge 22° / trend 310°');
        expect(t2).toContain('no depth');
        expect(t2).toContain('33333333');
    });

    it('fires onPointClick with the correct source_row_id on click', () => {
        const handler = vi.fn();
        render(<StereonetCard meta={META} onPointClick={handler} />);

        fireEvent.click(screen.getByTestId('stereonet-point-1'));
        expect(handler).toHaveBeenCalledTimes(1);
        expect(handler).toHaveBeenCalledWith('22222222-2222-2222-2222-222222222222');

        fireEvent.click(screen.getByTestId('stereonet-point-0'));
        expect(handler).toHaveBeenCalledTimes(2);
        expect(handler).toHaveBeenLastCalledWith('11111111-1111-1111-1111-111111111111');
    });

    it('does not throw when onPointClick is omitted', () => {
        render(<StereonetCard meta={META} />);
        expect(() => fireEvent.click(screen.getByTestId('stereonet-point-0'))).not.toThrow();
    });

    it('shows the hover tooltip when a point is focused', () => {
        render(<StereonetCard meta={META} />);
        fireEvent.mouseEnter(screen.getByTestId('stereonet-point-0'));
        const tooltip = screen.getByTestId('stereonet-tooltip');
        expect(tooltip.textContent ?? '').toContain('foliation');
        expect(tooltip.textContent ?? '').toContain('strike 45° / dip 60°');
    });
});
