import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import LayerTogglePanel from '../LayerTogglePanel';
import { LAYER_SPECS, type LayerId } from '../publicGeoscienceLayers';

// ── Default props factory ─────────────────────────────────────────────────

function makeLayerVisibility(value = true): Record<LayerId, boolean> {
    return Object.fromEntries(
        LAYER_SPECS.map(s => [s.id, value]),
    ) as Record<LayerId, boolean>;
}

const defaultProps = {
    layerVisibility: makeLayerVisibility(true),
    onToggleLayer: vi.fn(),
    commodityGrouping: null as string | null,
    onCommoditySelect: vi.fn(),
};

function renderPanel(props: Partial<typeof defaultProps> = {}) {
    const merged = { ...defaultProps, ...props };
    return render(<LayerTogglePanel {...merged} />);
}

// ── Layer checkboxes ──────────────────────────────────────────────────────

describe('LayerTogglePanel — layer checkboxes', () => {
    it('renders one checkbox per LAYER_SPEC', () => {
        renderPanel();
        // Tier 1 ships with 4 layers; Tier 1 expansion (rock samples,
        // assessment surveys) and Tier 2 (mineral disposition) add more.
        // Count is derived from the imported LAYER_SPECS so the test
        // holds as more canonical types are onboarded.
        const checkboxes = screen.getAllByRole('checkbox');
        expect(checkboxes).toHaveLength(LAYER_SPECS.length);
        expect(checkboxes.length).toBeGreaterThanOrEqual(4);
    });

    it('renders a label for each layer spec', () => {
        renderPanel();
        for (const spec of LAYER_SPECS) {
            expect(screen.getByText(spec.label)).toBeTruthy();
        }
    });

    it('renders the description for each layer spec', () => {
        renderPanel();
        for (const spec of LAYER_SPECS) {
            expect(screen.getByText(spec.description)).toBeTruthy();
        }
    });

    it('checkboxes reflect the layerVisibility prop (all true)', () => {
        renderPanel({ layerVisibility: makeLayerVisibility(true) });
        const checkboxes = screen.getAllByRole('checkbox') as HTMLInputElement[];
        for (const cb of checkboxes) {
            expect(cb.checked).toBe(true);
        }
    });

    it('checkboxes reflect the layerVisibility prop (all false)', () => {
        renderPanel({ layerVisibility: makeLayerVisibility(false) });
        const checkboxes = screen.getAllByRole('checkbox') as HTMLInputElement[];
        for (const cb of checkboxes) {
            expect(cb.checked).toBe(false);
        }
    });

    it('clicking a layer checkbox fires onToggleLayer with the layer id', () => {
        const onToggleLayer = vi.fn();
        renderPanel({ onToggleLayer });

        // Click the checkbox for each layer spec and verify the correct id
        const checkboxes = screen.getAllByRole('checkbox');
        fireEvent.click(checkboxes[0]);
        expect(onToggleLayer).toHaveBeenCalledWith(LAYER_SPECS[0].id);
    });

    it('clicking each layer checkbox fires onToggleLayer with that layer id', () => {
        const onToggleLayer = vi.fn();
        renderPanel({ onToggleLayer });

        const checkboxes = screen.getAllByRole('checkbox');
        LAYER_SPECS.forEach((spec, i) => {
            fireEvent.click(checkboxes[i]);
            expect(onToggleLayer).toHaveBeenCalledWith(spec.id);
        });
        expect(onToggleLayer).toHaveBeenCalledTimes(LAYER_SPECS.length);
    });
});

// ── Live badge ────────────────────────────────────────────────────────────

describe('LayerTogglePanel — Live badge', () => {
    it('renders the "Live" badge in the Layers section header', () => {
        renderPanel();
        // The badge text is "Live" (uppercase via CSS, but text content is "Live")
        expect(screen.getByText('Live')).toBeTruthy();
    });
});

// ── Commodity chips ───────────────────────────────────────────────────────

const EXPECTED_COMMODITY_IDS = [
    'precious_metals',
    'base_metals',
    'uranium',
    'potash_salt',
    'lithium',
    'ree',
    'industrial_materials',
    'coal',
    'gemstones',
];

const COMMODITY_LABELS: Record<string, string> = {
    precious_metals:      'Precious',
    base_metals:          'Base',
    uranium:              'Uranium',
    potash_salt:          'Potash/Salt',
    lithium:              'Lithium',
    ree:                  'REE',
    industrial_materials: 'Industrial',
    coal:                 'Coal',
    gemstones:            'Gemstones',
};

describe('LayerTogglePanel — commodity chips', () => {
    it('renders all 9 commodity chip buttons', () => {
        renderPanel();
        for (const id of EXPECTED_COMMODITY_IDS) {
            const label = COMMODITY_LABELS[id];
            // Each chip is a button containing the label text
            const buttons = screen.getAllByRole('button');
            const chip = buttons.find(b => b.textContent?.includes(label));
            expect(chip, `Expected chip for ${id} (label: "${label}") to exist`).toBeTruthy();
        }
    });

    it('clicking a commodity chip fires onCommoditySelect with the chip id', () => {
        const onCommoditySelect = vi.fn();
        renderPanel({ onCommoditySelect });

        const buttons = screen.getAllByRole('button');
        const uraniumChip = buttons.find(b => b.textContent?.includes('Uranium'));
        expect(uraniumChip).toBeTruthy();
        fireEvent.click(uraniumChip!);

        expect(onCommoditySelect).toHaveBeenCalledWith('uranium');
    });

    it('clicking an already-selected chip fires onCommoditySelect with null (toggle-off)', () => {
        const onCommoditySelect = vi.fn();
        renderPanel({ commodityGrouping: 'uranium', onCommoditySelect });

        const buttons = screen.getAllByRole('button');
        const uraniumChip = buttons.find(b => b.textContent?.includes('Uranium'));
        expect(uraniumChip).toBeTruthy();
        fireEvent.click(uraniumChip!);

        expect(onCommoditySelect).toHaveBeenCalledWith(null);
    });

    it('clicking an unselected chip when another is selected fires with the new chip id', () => {
        const onCommoditySelect = vi.fn();
        renderPanel({ commodityGrouping: 'uranium', onCommoditySelect });

        const buttons = screen.getAllByRole('button');
        const goldChip = buttons.find(b => b.textContent?.includes('Precious'));
        expect(goldChip).toBeTruthy();
        fireEvent.click(goldChip!);

        expect(onCommoditySelect).toHaveBeenCalledWith('precious_metals');
    });

    it('all 9 chip ids are covered', () => {
        // Verify count of distinct chips — the panel renders exactly 9 (plus 4
        // layer labels in the legend section, but those are <li> not buttons).
        renderPanel();
        const chipLabels = Object.values(COMMODITY_LABELS);
        const buttons = screen.getAllByRole('button');
        const chipButtons = buttons.filter(b =>
            chipLabels.some(label => b.textContent?.includes(label)),
        );
        // Each label appears exactly once in the chip section
        expect(chipButtons).toHaveLength(9);
    });
});

// ── Clear button ──────────────────────────────────────────────────────────

describe('LayerTogglePanel — Clear button', () => {
    it('does not render Clear button when commodityGrouping is null', () => {
        renderPanel({ commodityGrouping: null });
        expect(screen.queryByRole('button', { name: /Clear/i })).toBeNull();
    });

    it('renders Clear button when commodityGrouping is set', () => {
        renderPanel({ commodityGrouping: 'uranium' });
        expect(screen.getByRole('button', { name: /Clear/i })).toBeTruthy();
    });

    it('clicking Clear fires onCommoditySelect(null)', () => {
        const onCommoditySelect = vi.fn();
        renderPanel({ commodityGrouping: 'uranium', onCommoditySelect });

        fireEvent.click(screen.getByRole('button', { name: /Clear/i }));
        expect(onCommoditySelect).toHaveBeenCalledWith(null);
    });
});

// ── Legend section ────────────────────────────────────────────────────────

describe('LayerTogglePanel — legend', () => {
    it('renders the "Legend" heading', () => {
        renderPanel();
        expect(screen.getByText('Legend')).toBeTruthy();
    });

    it('renders 9 legend list items (one per commodity)', () => {
        renderPanel();
        // Each legend item is an <li> containing the label text
        const legendItems = EXPECTED_COMMODITY_IDS.map(id => {
            const label = COMMODITY_LABELS[id];
            // Get all text nodes with the commodity label — legend items
            // use the same text as chips.
            return screen.getAllByText(label);
        });
        // Each commodity label should appear at least once (chip) and at
        // least once more (legend) = at least 2 total.
        for (const items of legendItems) {
            expect(items.length).toBeGreaterThanOrEqual(2);
        }
    });

    it('legend swatches are colored spans with aria-hidden', () => {
        renderPanel();
        const hiddenSpans = document.querySelectorAll('span[aria-hidden="true"]');
        // Each commodity gets a colored span swatch in both the chip and legend.
        // 9 chips + 9 legend swatches = 18 aria-hidden color swatches.
        expect(hiddenSpans.length).toBeGreaterThanOrEqual(18);
    });

    it('legend swatch for precious_metals has the correct background color', () => {
        renderPanel();
        // The precious_metals color is amber-500 = #eab308
        const swatches = Array.from(
            document.querySelectorAll('span[aria-hidden="true"]'),
        ) as HTMLElement[];
        const amberSwatch = swatches.find(
            s => s.style.backgroundColor === 'rgb(234, 179, 8)' ||
                 s.getAttribute('style')?.includes('eab308'),
        );
        expect(amberSwatch).toBeTruthy();
    });
});

// ── Stateless contract (no internal state mutations) ──────────────────────

describe('LayerTogglePanel — stateless contract', () => {
    it('selected commodity chip has active ring class', () => {
        renderPanel({ commodityGrouping: 'uranium' });

        const buttons = screen.getAllByRole('button');
        const uraniumChip = buttons.find(b => b.textContent?.includes('Uranium'));
        expect(uraniumChip!.className).toContain('border-amber-500');
        expect(uraniumChip!.className).toContain('ring-1');
    });

    it('unselected commodity chip does not have active ring class', () => {
        renderPanel({ commodityGrouping: null });

        const buttons = screen.getAllByRole('button');
        const uraniumChip = buttons.find(b => b.textContent?.includes('Uranium'));
        expect(uraniumChip!.className).not.toContain('ring-amber-500');
    });
});
