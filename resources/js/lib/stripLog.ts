/**
 * Strip-log helpers: payload types, colours, lanes and tooltip text.
 *
 * Pure functions, shared by every surface that draws a hole's geology (the
 * Workspace LOGS column, the hole page, the chat inline strip log), so a
 * lithology code is the same colour everywhere and a band says the same thing
 * wherever it is hovered.
 *
 * COLOUR RULE. A band's colour is the data's own display colour when it gave
 * one as a hex code (`#8899aa`); otherwise it is a STABLE legend colour derived
 * from the code, so `GRN` is the same colour on every hole and every reload.
 * A colour DESCRIBED in words ("dark grey", "red-brown") is not a display
 * colour and is never turned into one - it is shown in the tooltip instead.
 * Nothing here knows what a rock code means: there is no per-dataset table of
 * codes to colours, and no code is assumed to be a particular rock.
 */

export interface LithologyDetail {
    description?: string | null;
    colour?: string | null;
    grain_size?: string | null;
    hardness?: string | null;
    weathering?: string | null;
    rqd?: number | null;
    recovery?: number | null;
}

export interface StripLithologyBand {
    from: number;
    to: number;
    code: string;
    label: string;
    color: string;
    detail?: LithologyDetail;
}

export interface AlterationItem {
    type: string;
    intensity: string | null;
    minerals: string[];
    notes: string | null;
}

export interface StripAlterationBand {
    from: number;
    to: number;
    label: string;
    alterations: AlterationItem[];
}

export interface StripMineralBand {
    from: number;
    to: number;
    mineral: string;
    abundance_pct: number | null;
    form: string | null;
    grain_size: string | null;
    notes: string | null;
}

export interface StripTracks {
    lithology: StripLithologyBand[];
    alteration: StripAlterationBand[];
    mineralization: StripMineralBand[];
    truncated?: { lithology: boolean; alteration: boolean; mineralization: boolean };
}

export const EMPTY_TRACKS: StripTracks = { lithology: [], alteration: [], mineralization: [] };

const HEX_COLOUR = /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i;

/** True for `#rgb` / `#rrggbb` - the only thing treated as a display colour. */
export function isDisplayColour(value: unknown): value is string {
    return typeof value === 'string' && HEX_COLOUR.test(value.trim());
}

/** HSL to `#rrggbb`. */
function hsl(h: number, sat: number, light: number): string {
    const c = (1 - Math.abs(2 * light - 1)) * sat;
    const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
    const m = light - c / 2;
    const [r, g, b] =
        h < 60
            ? [c, x, 0]
            : h < 120
              ? [x, c, 0]
              : h < 180
                ? [0, c, x]
                : h < 240
                  ? [0, x, c]
                  : h < 300
                    ? [x, 0, c]
                    : [c, 0, x];
    return `#${[r, g, b]
        .map((v) =>
            Math.round((v + m) * 255)
                .toString(16)
                .padStart(2, '0'),
        )
        .join('')}`;
}

/**
 * Categorical palette: 24 muted colours spaced by the golden angle, so any two
 * neighbours in the list are far apart in hue, alternating two tones. Muted
 * enough for dark and light themes, and deliberately not tied to any rock type.
 */
const PALETTE: readonly string[] = Array.from({ length: 24 }, (_, i) =>
    hsl((i * 137.508) % 360, i % 2 === 0 ? 0.36 : 0.46, i % 2 === 0 ? 0.62 : 0.52),
);

/** FNV-1a: small, deterministic, no dependency. */
function hash(text: string): number {
    let h = 0x811c9dc5;
    for (let i = 0; i < text.length; i += 1) {
        h ^= text.charCodeAt(i);
        h = Math.imul(h, 0x01000193);
    }
    return h >>> 0;
}

/** Same key, same colour - across holes, sessions and surfaces. */
export function stableColour(key: string, offset = 0): string {
    const normalised = key.trim().toUpperCase();
    return PALETTE[(hash(normalised) + offset) % PALETTE.length];
}

/** Lithology fill: the data's hex colour if it has one, else the code's legend colour. */
export function lithologyColour(code: string, hint?: string | null): string {
    if (isDisplayColour(hint)) return hint.trim().toLowerCase();
    return stableColour(code || '?');
}

/**
 * Colours for the categories of ONE hole (or one panel), first-seen order.
 *
 * A category's colour starts at its stable hash slot; if another category of
 * the same hole already holds it, the next free slot is taken, so two codes on
 * one strip log are never the same swatch (a hash into 24 slots collides for
 * about half of all 6-code holes, and a legend of identical swatches says
 * nothing). Data-given hex colours reserve their slot first. The only cost is
 * that a colliding code may differ from its colour on another hole, which the
 * per-hole legend states.
 */
export function categoryColours(entries: { key: string; hint?: string | null }[], offset = 0): Map<string, string> {
    const out = new Map<string, string>();
    const taken = new Set<string>();
    for (const { key, hint } of entries) {
        if (!out.has(key) && isDisplayColour(hint)) {
            const colour = hint.trim().toLowerCase();
            out.set(key, colour);
            taken.add(colour);
        }
    }
    for (const { key } of entries) {
        if (out.has(key)) continue;
        const start = (hash(key.trim().toUpperCase()) + offset) % PALETTE.length;
        let colour = PALETTE[start];
        for (let step = 1; taken.has(colour) && step < PALETTE.length; step += 1) {
            colour = PALETTE[(start + step) % PALETTE.length];
        }
        out.set(key, colour);
        taken.add(colour);
    }
    return out;
}

/** Colours for the lithology codes of one hole. */
export function lithologyColourMap(bands: { code: string; color?: string | null }[]): Map<string, string> {
    return categoryColours(bands.map((b) => ({ key: b.code || '?', hint: b.color })));
}

/** Colours for the alteration types / minerals of one hole. */
export function alterationColourMap(types: string[]): Map<string, string> {
    return categoryColours(
        types.map((key) => ({ key })),
        5,
    );
}

export function mineralColourMap(minerals: string[]): Map<string, string> {
    return categoryColours(
        minerals.map((key) => ({ key })),
        11,
    );
}

/** Black or white, whichever reads on *fill*. Non-hex input falls back to dark text. */
export function readableOn(fill: string): string {
    if (!isDisplayColour(fill)) return '#111827';
    let hex = fill.trim().slice(1);
    if (hex.length === 3)
        hex = hex
            .split('')
            .map((c) => c + c)
            .join('');
    const [r, g, b] = [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
    const luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b;
    return luminance > 0.55 ? '#111827' : '#f9fafb';
}

/** A darker edge for a fill. */
export function edgeOf(fill: string): string {
    if (!isDisplayColour(fill)) return 'rgba(0,0,0,0.35)';
    let hex = fill.trim().slice(1);
    if (hex.length === 3)
        hex = hex
            .split('')
            .map((c) => c + c)
            .join('');
    const parts = [0, 2, 4].map((i) => Math.round(parseInt(hex.slice(i, i + 2), 16) * 0.65));
    return `#${parts.map((p) => p.toString(16).padStart(2, '0')).join('')}`;
}

export interface Laned<T> {
    item: T;
    lane: number;
}

/**
 * Greedy lane assignment for intervals that may overlap (two alterations or
 * minerals over the same metres): each interval takes the first lane whose last
 * interval has already ended. Input order is preserved in the output.
 */
export function packLanes<T extends { from: number; to: number }>(items: T[]): { placed: Laned<T>[]; lanes: number } {
    const order = items
        .map((item, index) => ({ item, index }))
        .sort((a, b) => a.item.from - b.item.from || a.item.to - b.item.to || a.index - b.index);
    const laneEnds: number[] = [];
    const laneOf = new Map<number, number>();
    for (const { item, index } of order) {
        let lane = laneEnds.findIndex((end) => end <= item.from);
        if (lane === -1) {
            lane = laneEnds.length;
            laneEnds.push(item.to);
        } else {
            laneEnds[lane] = item.to;
        }
        laneOf.set(index, lane);
    }
    return {
        placed: items.map((item, index) => ({ item, lane: laneOf.get(index) ?? 0 })),
        lanes: Math.max(1, laneEnds.length),
    };
}

const fmt = (n: number): string => (Number.isInteger(n) ? String(n) : n.toFixed(1));

export function depthRange(from: number, to: number): string {
    return `${fmt(from)}-${fmt(to)} m`;
}

/** Tooltip / detail lines for a lithology band, most useful first. */
export function lithologyLines(band: StripLithologyBand): string[] {
    const lines = [`${band.code || '?'}  ${depthRange(band.from, band.to)}`];
    const description = band.detail?.description || band.label;
    if (description && description !== band.code) lines.push(description);
    const d = band.detail;
    if (d) {
        if (d.colour) lines.push(`Colour: ${d.colour}`);
        if (d.grain_size) lines.push(`Grain size: ${d.grain_size}`);
        if (d.hardness) lines.push(`Hardness: ${d.hardness}`);
        if (d.weathering) lines.push(`Weathering: ${d.weathering}`);
        if (d.rqd != null) lines.push(`RQD: ${fmt(d.rqd)}%`);
        if (d.recovery != null) lines.push(`Recovery: ${fmt(d.recovery)}%`);
    }
    return lines;
}

export function alterationItemText(item: AlterationItem): string {
    return item.intensity ? `${item.type} (${item.intensity})` : item.type;
}

export function alterationLines(band: StripAlterationBand): string[] {
    const lines = [`Alteration  ${depthRange(band.from, band.to)}`];
    for (const item of band.alterations) {
        lines.push(alterationItemText(item));
        if (item.minerals.length) lines.push(`  minerals: ${item.minerals.join(', ')}`);
        if (item.notes) lines.push(`  ${item.notes}`);
    }
    if (band.alterations.length === 0 && band.label) lines.push(band.label);
    return lines;
}

/** "Pyrite 3%" or "Pyrite" - the percentage only when the data gave a number. */
export function mineralText(band: StripMineralBand): string {
    return band.abundance_pct != null ? `${band.mineral} ${fmt(band.abundance_pct)}%` : band.mineral;
}

export function mineralLines(band: StripMineralBand): string[] {
    const lines = [`${mineralText(band)}  ${depthRange(band.from, band.to)}`];
    if (band.form) lines.push(`Style: ${band.form}`);
    if (band.grain_size) lines.push(`Grain size: ${band.grain_size}`);
    if (band.notes) lines.push(band.notes);
    return lines;
}

/** Unique lithology codes in first-seen order, with the colour each draws in. */
export function lithologyLegend(bands: StripLithologyBand[]): { code: string; colour: string; label: string }[] {
    const seen = new Map<string, { code: string; colour: string; label: string }>();
    const colours = lithologyColourMap(bands);
    for (const band of bands) {
        const code = band.code || '?';
        if (!seen.has(code)) {
            seen.set(code, {
                code,
                colour: colours.get(code) ?? lithologyColour(code, band.color),
                // The first description logged for the code - what the geologist called it.
                label: band.detail?.description || (band.label !== code ? band.label : ''),
            });
        }
    }
    return [...seen.values()];
}

/** A gold `drillhole_intervals_visual` row, as the hole page's older payload carries it. */
export interface GoldIntervalRow {
    depth_from: number;
    depth_to: number;
    interval_kind: string;
    lithology_code?: string | null;
    lithology_label?: string | null;
    color_hint?: string | null;
    /** JSONB: an object, or the JSON text of one, depending on the driver. */
    alteration_payload?: unknown;
    mineralization_payload?: unknown;
}

function payloadList(payload: unknown, key: string): unknown[] {
    let value = payload;
    if (typeof value === 'string') {
        try {
            value = JSON.parse(value);
        } catch {
            return [];
        }
    }
    const list = value && typeof value === 'object' ? (value as Record<string, unknown>)[key] : undefined;
    return Array.isArray(list) ? list : [];
}

const textOrNull = (v: unknown): string | null => (v === null || v === undefined ? null : String(v));

/**
 * Bands from the gold interval rows the hole page already had, for an older
 * payload. Lithology, alteration and mineralization rows are understood; a gold
 * `mineralization` row is one interval carrying every mineral in its payload
 * and is flattened to one band per mineral (the shape `strip_tracks` has).
 */
export function tracksFromIntervals(intervals: GoldIntervalRow[]): StripTracks {
    const alteration: StripAlterationBand[] = [];
    const mineralization: StripMineralBand[] = [];
    for (const i of intervals) {
        const from = Number(i.depth_from);
        const to = Number(i.depth_to);
        if (i.interval_kind === 'alteration') {
            const alterations: AlterationItem[] = [];
            for (const raw of payloadList(i.alteration_payload, 'alterations')) {
                const a = raw as Record<string, unknown> | null;
                if (!a || a.type === undefined || a.type === null) continue;
                alterations.push({
                    type: String(a.type),
                    intensity: textOrNull(a.intensity),
                    minerals: Array.isArray(a.minerals) ? a.minerals.map(String) : [],
                    notes: textOrNull(a.notes),
                });
            }
            alteration.push({ from, to, label: i.lithology_label ?? '', alterations });
        } else if (i.interval_kind === 'mineralization') {
            for (const raw of payloadList(i.mineralization_payload, 'minerals')) {
                const m = raw as Record<string, unknown> | null;
                if (!m || m.mineral === undefined || m.mineral === null) continue;
                const pct = m.abundance_pct === null || m.abundance_pct === undefined ? NaN : Number(m.abundance_pct);
                mineralization.push({
                    from,
                    to,
                    mineral: String(m.mineral),
                    abundance_pct: Number.isFinite(pct) ? pct : null,
                    form: textOrNull(m.form),
                    grain_size: textOrNull(m.grain_size),
                    notes: textOrNull(m.notes),
                });
            }
        }
    }
    return {
        lithology: intervals
            .filter((i) => i.interval_kind === 'lithology')
            .map((i) => ({
                from: Number(i.depth_from),
                to: Number(i.depth_to),
                code: i.lithology_code ?? '',
                label: i.lithology_label ?? '',
                color: isDisplayColour(i.color_hint) ? i.color_hint : '',
            })),
        alteration,
        mineralization,
    };
}
