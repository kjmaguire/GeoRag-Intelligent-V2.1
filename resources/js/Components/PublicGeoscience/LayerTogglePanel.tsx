import { cn } from '@/lib/utils';
import { Badge } from '@/Components/ui/badge';
import { LAYER_SPECS, type LayerId, GROUPING_COLORS } from './publicGeoscienceLayers';

/**
 * Right-rail layer toggle panel.
 *
 * - Four per-layer checkboxes (mines / occurrences / drillholes / resource
 *   potential) wired to the parent's layerVisibility state.
 * - Commodity grouping filter — one-of buttons that apply a MapLibre filter
 *   across all layers (plan §09a: commodity filter applies across layers).
 *
 * Stateless: state lives in /public-geoscience/Index.tsx so the map component
 * is the single consumer. This panel is pure props-in / callbacks-out.
 */

interface LayerTogglePanelProps {
    layerVisibility: Record<LayerId, boolean>;
    onToggleLayer: (id: LayerId) => void;
    commodityGrouping: string | null;
    onCommoditySelect: (grouping: string | null) => void;
}

interface CommodityChip {
    id: string; // commodity_grouping enum value
    label: string;
}

const COMMODITY_CHIPS: CommodityChip[] = [
    { id: 'precious_metals',      label: 'Precious' },
    { id: 'base_metals',          label: 'Base' },
    { id: 'uranium',              label: 'Uranium' },
    { id: 'potash_salt',          label: 'Potash/Salt' },
    { id: 'lithium',              label: 'Lithium' },
    { id: 'ree',                  label: 'REE' },
    { id: 'industrial_materials', label: 'Industrial' },
    { id: 'coal',                 label: 'Coal' },
    { id: 'gemstones',            label: 'Gemstones' },
];

export default function LayerTogglePanel({
    layerVisibility,
    onToggleLayer,
    commodityGrouping,
    onCommoditySelect,
}: LayerTogglePanelProps) {
    return (
        <div className="flex flex-col gap-4 p-3 overflow-y-auto">
            <section>
                <div className="flex items-center justify-between mb-2">
                    <h2 className="text-sm font-semibold text-gray-100">Layers</h2>
                    <Badge className="bg-amber-600/20 text-amber-300 border-amber-700 text-[10px] uppercase tracking-wider">
                        Live
                    </Badge>
                </div>
                <ul className="flex flex-col gap-2">
                    {LAYER_SPECS.map((spec) => {
                        const checked = layerVisibility[spec.id];
                        return (
                            <li key={spec.id}>
                                <label
                                    className={cn(
                                        'flex items-start gap-3 rounded-md border px-3 py-2 cursor-pointer transition-colors',
                                        checked
                                            ? 'border-amber-700/50 bg-amber-950/20 hover:bg-amber-950/30'
                                            : 'border-gray-800 bg-gray-950/50 hover:bg-gray-900/50',
                                    )}
                                >
                                    <input
                                        type="checkbox"
                                        checked={checked}
                                        onChange={() => onToggleLayer(spec.id)}
                                        className="mt-1 accent-amber-500"
                                    />
                                    <div className="flex flex-col">
                                        <span className="text-sm text-gray-100">{spec.label}</span>
                                        <span className="text-xs text-gray-500 leading-snug">
                                            {spec.description}
                                        </span>
                                    </div>
                                </label>
                            </li>
                        );
                    })}
                </ul>
            </section>

            <section>
                <div className="flex items-center justify-between mb-2">
                    <h2 className="text-sm font-semibold text-gray-100">Commodity</h2>
                    {commodityGrouping && (
                        <button
                            type="button"
                            onClick={() => onCommoditySelect(null)}
                            className="text-[11px] text-gray-500 hover:text-amber-300 underline"
                        >
                            Clear
                        </button>
                    )}
                </div>
                <p className="text-[11px] text-gray-500 leading-snug mb-2">
                    Filters mines, occurrences, and drillholes by commodity
                    grouping. Does not affect resource potential polygons
                    (grouping is per-polygon on that layer).
                </p>
                <div className="flex flex-wrap gap-1.5">
                    {COMMODITY_CHIPS.map((chip) => {
                        const active = commodityGrouping === chip.id;
                        const swatch = GROUPING_COLORS[chip.id] ?? '#6b7280';
                        return (
                            <button
                                key={chip.id}
                                type="button"
                                onClick={() =>
                                    onCommoditySelect(active ? null : chip.id)
                                }
                                className={cn(
                                    'inline-flex items-center gap-1.5 px-2 py-1 rounded-md border text-[11px] transition-colors',
                                    active
                                        ? 'border-amber-500 bg-amber-950/40 text-amber-200 ring-1 ring-amber-500'
                                        : 'border-gray-800 bg-gray-950/50 text-gray-400 hover:border-gray-700 hover:text-gray-200',
                                )}
                            >
                                <span
                                    aria-hidden="true"
                                    className="w-2 h-2 rounded-full"
                                    style={{ backgroundColor: swatch }}
                                />
                                {chip.label}
                            </button>
                        );
                    })}
                </div>
            </section>

            <section className="mt-auto pt-3 border-t border-gray-800">
                <div className="flex items-center justify-between mb-2">
                    <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
                        Legend
                    </h3>
                </div>
                <ul className="flex flex-col gap-1">
                    {COMMODITY_CHIPS.map((chip) => (
                        <li key={chip.id} className="flex items-center gap-2 text-[11px] text-gray-500">
                            <span
                                aria-hidden="true"
                                className="w-2.5 h-2.5 rounded-full"
                                style={{ backgroundColor: GROUPING_COLORS[chip.id] ?? '#6b7280' }}
                            />
                            {chip.label}
                        </li>
                    ))}
                </ul>
            </section>
        </div>
    );
}
