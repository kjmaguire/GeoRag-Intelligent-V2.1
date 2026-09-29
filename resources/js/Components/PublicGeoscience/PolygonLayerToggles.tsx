import {
    POLYGON_LAYER_COLORS,
    POLYGON_LAYER_KEYS,
    POLYGON_LAYER_LABELS,
    polygonLayerStatus,
    type PolygonLayerKey,
    type PolygonLayerMeta,
} from './polygonLayers';

interface Props {
    enabled: PolygonLayerKey[];
    onChange: (next: PolygonLayerKey[]) => void;
    meta?: Partial<Record<PolygonLayerKey, PolygonLayerMeta>>;
}

/**
 * Checkbox legend for the polygon overlays. Doubles as the legend (swatch =
 * fill colour) and as the place each layer's server-side status is told —
 * "zoom in" below its minimum zoom, "N of M" when the per-layer cap bit —
 * so a clipped overlay never passes for a complete one.
 */
export default function PolygonLayerToggles({ enabled, onChange, meta }: Props) {
    const toggle = (key: PolygonLayerKey) => {
        onChange(
            enabled.includes(key)
                ? enabled.filter((k) => k !== key)
                : POLYGON_LAYER_KEYS.filter((k) => k === key || enabled.includes(k)),
        );
    };

    return (
        <fieldset className="flex items-center gap-3 flex-wrap text-[10px] font-mono" style={{ color: 'var(--fg-3)' }}>
            <legend className="sr-only">Polygon layers</legend>
            {POLYGON_LAYER_KEYS.map((key) => {
                const on = enabled.includes(key);
                const status = on ? polygonLayerStatus(meta?.[key]) : null;
                return (
                    <label key={key} className="flex items-center gap-1.5 cursor-pointer">
                        <input
                            type="checkbox"
                            checked={on}
                            onChange={() => toggle(key)}
                            aria-label={POLYGON_LAYER_LABELS[key]}
                        />
                        <span
                            className="w-3 h-2 inline-block border"
                            style={{
                                background: `${POLYGON_LAYER_COLORS[key]}55`,
                                borderColor: POLYGON_LAYER_COLORS[key],
                            }}
                            aria-hidden="true"
                        />
                        {POLYGON_LAYER_LABELS[key]}
                        {status && (
                            <span
                                className={meta?.[key]?.truncated || meta?.[key]?.mode === 'min_zoom' ? 'text-amber-400' : ''}
                            >
                                ({status})
                            </span>
                        )}
                    </label>
                );
            })}
        </fieldset>
    );
}
