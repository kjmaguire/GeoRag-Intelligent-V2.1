import { describe, expect, it } from 'vitest';
import {
    alterationColourMap,
    alterationLines,
    depthRange,
    isDisplayColour,
    lithologyColour,
    lithologyColourMap,
    lithologyLegend,
    lithologyLines,
    mineralColourMap,
    mineralLines,
    mineralText,
    packLanes,
    readableOn,
    stableColour,
    tracksFromIntervals,
} from '../stripLog';

describe('strip log colours', () => {
    it('takes only a hex code as a display colour', () => {
        expect(isDisplayColour('#8899aa')).toBe(true);
        expect(isDisplayColour('#abc')).toBe(true);
        expect(isDisplayColour(' #8899AA ')).toBe(true);
        // A colour described in words is not a display colour, and neither is a rock code.
        for (const notHex of ['dark grey', 'grey', 'GRN', '', null, undefined, '#12345', '#gggggg']) {
            expect(isDisplayColour(notHex)).toBe(false);
        }
    });

    it("uses the data's hex colour when it has one, lower-cased", () => {
        expect(lithologyColour('GRN', '#8899AA')).toBe('#8899aa');
    });

    it('falls back to a stable legend colour per code, never to the colour text', () => {
        const a = lithologyColour('GRN', 'dark grey');
        expect(a).toMatch(/^#[0-9a-f]{6}$/);
        expect(lithologyColour('GRN', null)).toBe(a);
        expect(lithologyColour('GRN', 'red-brown')).toBe(a);
        // Same code, same colour, whatever its case or padding.
        expect(lithologyColour(' grn ')).toBe(a);
    });

    it('does not know what any code means: there is no per-dataset table', () => {
        // The codes the old hard-coded palette special-cased get the same treatment as any other.
        for (const code of ['SST', 'CGL', 'PGN', 'GPT']) {
            expect(lithologyColour(code)).toBe(stableColour(code));
        }
    });

    it('never gives two codes of one hole the same swatch', () => {
        // A hash into a fixed palette collides for about half of all 6-code holes.
        const codes = ['GRN', 'SST', 'MDS', 'SHL', 'QTZ', 'BAS', 'DIO', 'GNS', 'AND', 'TUF', 'LST', 'DOL'];
        const colours = lithologyColourMap(codes.map((code) => ({ code })));
        expect(new Set(colours.values()).size).toBe(codes.length);
    });

    it('is deterministic for the same hole', () => {
        const bands = [{ code: 'GRN' }, { code: 'SST' }, { code: 'MDS' }];
        expect([...lithologyColourMap(bands)]).toEqual([...lithologyColourMap(bands)]);
    });

    it('lets a data hex colour reserve its swatch and keeps it exactly', () => {
        const stable = lithologyColour('SST');
        const colours = lithologyColourMap([{ code: 'GRN', color: stable }, { code: 'SST' }]);
        expect(colours.get('GRN')).toBe(stable);
        expect(colours.get('SST')).not.toBe(stable);
    });

    it('keeps alteration types and minerals distinct too', () => {
        const alt = alterationColourMap(['Chlorite', 'Sericite', 'Silica', 'Carbonate', 'Hematite']);
        expect(new Set(alt.values()).size).toBe(5);
        const min = mineralColourMap(['Pyrite', 'Chalcopyrite', 'Galena', 'Sphalerite', 'Pyrrhotite', 'Magnetite']);
        expect(new Set(min.values()).size).toBe(6);
    });

    it('picks readable text for a fill', () => {
        expect(readableOn('#ffffff')).toBe('#111827');
        expect(readableOn('#000000')).toBe('#f9fafb');
        expect(readableOn('not a colour')).toBe('#111827');
    });
});

describe('packLanes', () => {
    it('puts overlapping intervals in different lanes and reuses a lane once it is free', () => {
        const { placed, lanes } = packLanes([
            { from: 0, to: 10 },
            { from: 5, to: 15 },
            { from: 10, to: 20 },
        ]);
        expect(placed.map((p) => p.lane)).toEqual([0, 1, 0]);
        expect(lanes).toBe(2);
    });

    it('keeps the input order and handles no intervals', () => {
        const input = [{ from: 20, to: 30 }, { from: 0, to: 5 }];
        expect(packLanes(input).placed.map((p) => p.item)).toEqual(input);
        expect(packLanes([]).lanes).toBe(1);
    });
});

describe('tooltip text', () => {
    it('lists the attributes of a lithology band', () => {
        const lines = lithologyLines({
            from: 0, to: 5, code: 'GRN', label: 'Grey granite', color: '',
            detail: {
                description: 'Grey granite', colour: 'dark grey', grain_size: 'Fine', hardness: 'Hard',
                weathering: 'Fresh', rqd: 85, recovery: 98,
            },
        });
        expect(lines[0]).toBe('GRN  0-5 m');
        expect(lines).toEqual(expect.arrayContaining([
            'Grey granite', 'Colour: dark grey', 'Grain size: Fine', 'Hardness: Hard',
            'Weathering: Fresh', 'RQD: 85%', 'Recovery: 98%',
        ]));
    });

    it('does not repeat a label that is only the code', () => {
        expect(lithologyLines({ from: 0, to: 5, code: 'GRN', label: 'GRN', color: '' })).toEqual(['GRN  0-5 m']);
    });

    it('shows every alteration of an interval with its intensity and minerals', () => {
        const lines = alterationLines({
            from: 0, to: 5, label: '',
            alterations: [
                { type: 'Chlorite', intensity: 'Strong', minerals: ['chlorite', 'sericite'], notes: null },
                { type: 'Silica', intensity: null, minerals: [], notes: 'patchy' },
            ],
        });
        expect(lines).toEqual([
            'Alteration  0-5 m', 'Chlorite (Strong)', '  minerals: chlorite, sericite', 'Silica', '  patchy',
        ]);
    });

    it('writes a percentage only when the data gave a number', () => {
        const base = { from: 5, to: 10, form: 'Disseminated', grain_size: null, notes: 'abundance: trace' };
        expect(mineralText({ ...base, mineral: 'Pyrite', abundance_pct: 3 })).toBe('Pyrite 3%');
        expect(mineralText({ ...base, mineral: 'Pyrite', abundance_pct: 3.5 })).toBe('Pyrite 3.5%');
        expect(mineralText({ ...base, mineral: 'Pyrite', abundance_pct: null })).toBe('Pyrite');
        expect(mineralLines({ ...base, mineral: 'Pyrite', abundance_pct: null })).toEqual([
            'Pyrite  5-10 m', 'Style: Disseminated', 'abundance: trace',
        ]);
    });

    it('formats depth ranges', () => {
        expect(depthRange(0, 5.25)).toBe('0-5.3 m');
    });
});

describe('legend and fallbacks', () => {
    it('lists each code once, in the colour it is drawn in, with the first description', () => {
        const legend = lithologyLegend([
            { from: 0, to: 5, code: 'GRN', label: 'Grey granite', color: '#112233', detail: { description: 'Grey granite' } },
            { from: 5, to: 9, code: 'GRN', label: 'Other', color: '' },
            { from: 9, to: 12, code: 'SST', label: 'SST', color: '' },
        ]);
        expect(legend.map((l) => l.code)).toEqual(['GRN', 'SST']);
        expect(legend[0]).toMatchObject({ colour: '#112233', label: 'Grey granite' });
        expect(legend[1].label).toBe('');
    });

    it('builds lithology bands from gold interval rows for an older payload, dropping text colours', () => {
        const tracks = tracksFromIntervals([
            { depth_from: 0, depth_to: 5, interval_kind: 'lithology', lithology_code: 'GRN', lithology_label: 'x', color_hint: 'grey' },
            { depth_from: 5, depth_to: 9, interval_kind: 'lithology', lithology_code: 'SST', lithology_label: 'y', color_hint: '#abcdef' },
            { depth_from: 0, depth_to: 1, interval_kind: 'sample_window' },
        ]);
        expect(tracks.lithology.map((b) => b.color)).toEqual(['', '#abcdef']);
        expect(tracks.alteration).toEqual([]);
        expect(tracks.mineralization).toEqual([]);
    });

    it('flattens a gold mineralization row to one band per mineral, in payload order (§04e)', () => {
        const tracks = tracksFromIntervals([
            {
                depth_from: 5,
                depth_to: 10,
                interval_kind: 'mineralization',
                lithology_label: 'Pyrite 3%; Chalcopyrite',
                mineralization_payload: {
                    minerals: [
                        { mineral: 'Pyrite', abundance_pct: 3, form: 'Disseminated', grain_size: 'Fine', notes: 'vein-hosted' },
                        { mineral: 'Chalcopyrite', abundance_pct: null, form: null, grain_size: null, notes: null },
                    ],
                },
            },
            // JSONB can arrive as text, and a row with no readable minerals adds no band.
            { depth_from: 20, depth_to: 22, interval_kind: 'mineralization', mineralization_payload: '{"minerals":[{"mineral":"Galena","abundance_pct":"0.5"}]}' },
            { depth_from: 30, depth_to: 31, interval_kind: 'mineralization', mineralization_payload: {} },
            { depth_from: 40, depth_to: 41, interval_kind: 'mineralization', mineralization_payload: 'not json' },
        ]);
        expect(tracks.mineralization).toEqual([
            { from: 5, to: 10, mineral: 'Pyrite', abundance_pct: 3, form: 'Disseminated', grain_size: 'Fine', notes: 'vein-hosted' },
            { from: 5, to: 10, mineral: 'Chalcopyrite', abundance_pct: null, form: null, grain_size: null, notes: null },
            { from: 20, to: 22, mineral: 'Galena', abundance_pct: 0.5, form: null, grain_size: null, notes: null },
        ]);
        expect(tracks.lithology).toEqual([]);
    });

    it('builds alteration bands from gold alteration rows for an older payload', () => {
        const tracks = tracksFromIntervals([
            {
                depth_from: 0,
                depth_to: 5,
                interval_kind: 'alteration',
                lithology_label: 'Chlorite (Strong)',
                alteration_payload: { alterations: [{ type: 'Chlorite', intensity: 'Strong', minerals: ['chlorite'], notes: null }, { intensity: 'x' }] },
            },
        ]);
        expect(tracks.alteration).toEqual([
            { from: 0, to: 5, label: 'Chlorite (Strong)', alterations: [{ type: 'Chlorite', intensity: 'Strong', minerals: ['chlorite'], notes: null }] },
        ]);
    });
});
