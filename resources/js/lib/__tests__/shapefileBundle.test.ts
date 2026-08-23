import { describe, expect, it } from 'vitest';
import JSZip from 'jszip';
import {
    BUNDLE_MEMBER_EXTS,
    groupShapefiles,
    isMapInfoSidecar,
    isShapefileSidecar,
} from '@/lib/shapefileBundle';

/**
 * The bug these pin: a folder import queued `geology_poly.shp` on its own and
 * dropped its `.shx`/`.dbf`/`.prj` as "unsupported", because only `.shp` has an
 * upload category. Ingestion then failed with GDAL's
 * "Unable to open geology_poly.shx" for every shapefile in the project.
 */

function makeFile(name: string, dir = '', body = 'x'): File {
    const f = new File([body], name.split('/').pop() as string);
    if (dir) {
        Object.defineProperty(f, 'webkitRelativePath', {
            value: `${dir}/${f.name}`,
        });
    }
    return f;
}

async function membersOf(file: File): Promise<string[]> {
    const zip = await JSZip.loadAsync(await file.arrayBuffer());
    return Object.keys(zip.files).sort();
}

describe('groupShapefiles', () => {
    it('zips a shapefile with its sidecars instead of uploading the .shp alone', async () => {
        const { bundles, passthrough, unusable } = await groupShapefiles([
            makeFile('geology_poly.shp'),
            makeFile('geology_poly.shx'),
            makeFile('geology_poly.dbf'),
            makeFile('geology_poly.prj'),
        ]);

        expect(bundles).toHaveLength(1);
        expect(passthrough).toHaveLength(0);
        expect(unusable).toHaveLength(0);
        expect(bundles[0].kind).toBe('shapefile');
        expect(bundles[0].file.name).toBe('geology_poly.zip');
        expect(await membersOf(bundles[0].file)).toEqual([
            'geology_poly.dbf',
            'geology_poly.prj',
            'geology_poly.shp',
            'geology_poly.shx',
        ]);
        expect(bundles[0].missing).toEqual([]);
        expect(bundles[0].verdict).toBeNull();
    });

    it('reports which required sidecars were absent without refusing the upload', async () => {
        const { bundles } = await groupShapefiles([
            makeFile('faults.shp'),
            makeFile('faults.dbf'),
        ]);

        // GDAL rebuilds a missing index from the .shp itself, so .shx is not
        // worth a word — but a missing .prj means the file declares no CRS,
        // which the ingest now refuses outright, so it must be said here.
        expect(bundles).toHaveLength(1);
        expect(bundles[0].missing).toEqual(['shx', 'prj']);
        expect(bundles[0].verdict).toContain('no .prj');
        expect(bundles[0].verdict).toContain('EPSG');
    });

    it('says nothing about a missing .shx alone, because GDAL regenerates it', async () => {
        const { bundles } = await groupShapefiles([
            makeFile('claims.shp'),
            makeFile('claims.dbf'),
            makeFile('claims.prj'),
        ]);

        expect(bundles[0].missing).toEqual(['shx']);
        expect(bundles[0].verdict).toBeNull();
    });

    it('groups a mis-cased sidecar with its .shp so the CRS survives', async () => {
        // The real delivery has drobeck_shumagin_veins.shp beside
        // Drobeck_Shumagin_Veins.prj. GDAL on Linux is case-sensitive, so if
        // the .prj does not reach the ZIP the layer arrives with no CRS and
        // is rejected — with the coordinate system sitting on disk next to it.
        const { bundles, unusable } = await groupShapefiles([
            makeFile('drobeck_shumagin_veins.shp'),
            makeFile('Drobeck_Shumagin_Veins.prj'),
        ]);

        expect(unusable).toHaveLength(0);
        expect(bundles).toHaveLength(1);
        expect(await membersOf(bundles[0].file)).toEqual([
            'Drobeck_Shumagin_Veins.prj',
            'drobeck_shumagin_veins.shp',
        ]);
        expect(bundles[0].missing).not.toContain('prj');
    });

    it('keeps same-named shapefiles in different folders apart', async () => {
        const { bundles } = await groupShapefiles([
            makeFile('faults.shp', 'geology'),
            makeFile('faults.shx', 'geology'),
            makeFile('faults.shp', 'claims'),
            makeFile('faults.shx', 'claims'),
        ]);

        expect(bundles).toHaveLength(2);
        for (const b of bundles) {
            expect(await membersOf(b.file)).toEqual(['faults.shp', 'faults.shx']);
        }
    });

    it('leaves non-shapefile uploads alone', async () => {
        const files = [
            makeFile('report.pdf'),
            makeFile('collars.csv'),
            makeFile('map.tif'),
        ];
        const { bundles, passthrough } = await groupShapefiles(files);

        expect(bundles).toHaveLength(0);
        expect(passthrough.map((f) => f.name)).toEqual([
            'report.pdf',
            'collars.csv',
            'map.tif',
        ]);
    });

    it('uploads a standalone .dbf as an attribute table instead of discarding it', async () => {
        // This test used to assert the opposite: that a lone .dbf was an
        // unusable orphan. It was rewritten deliberately. pyogrio reads a bare
        // .dbf through the ESRI Shapefile driver — 10 rows, 9 columns, no
        // geometry — and a large part of the GIS world still hands over collar
        // and sample tables in exactly that form. Dropping them at the drop
        // zone meant the files could not be imported by any route at all.
        const { bundles, passthrough, unusable } = await groupShapefiles([
            makeFile('MiscPoints_2005.dbf'),
        ]);

        expect(bundles).toHaveLength(0);
        expect(unusable).toHaveLength(0);
        expect(passthrough.map((f) => f.name)).toEqual(['MiscPoints_2005.dbf']);
    });

    it('keeps a .dbf as a sidecar when its .shp is in the selection', async () => {
        // The discriminator is a same-stem .shp, and only that. Getting this
        // wrong the other way would upload every shapefile's attribute table
        // a second time as a headless table.
        const { bundles, passthrough } = await groupShapefiles([
            makeFile('veins.shp'),
            makeFile('veins.shx'),
            makeFile('veins.dbf'),
            makeFile('veins.prj'),
        ]);

        expect(passthrough).toHaveLength(0);
        expect(await membersOf(bundles[0].file)).toContain('veins.dbf');
    });

    it('does not absorb a same-named file that is not a sidecar', async () => {
        // faults.pdf is its own report, not part of the shapefile.
        const { bundles, passthrough } = await groupShapefiles([
            makeFile('faults.shp'),
            makeFile('faults.shx'),
            makeFile('faults.pdf'),
        ]);

        expect(await membersOf(bundles[0].file)).toEqual([
            'faults.shp',
            'faults.shx',
        ]);
        expect(passthrough.map((f) => f.name)).toEqual(['faults.pdf']);
    });

    it('bundles the .cpg so non-ASCII attribute values are not mangled', async () => {
        const { bundles } = await groupShapefiles([
            makeFile('claims.shp'),
            makeFile('claims.shx'),
            makeFile('claims.dbf'),
            makeFile('claims.prj'),
            makeFile('claims.cpg'),
        ]);

        expect(await membersOf(bundles[0].file)).toContain('claims.cpg');
    });

    it('matches sidecar extensions case-insensitively', async () => {
        const { bundles, unusable } = await groupShapefiles([
            makeFile('UPPER.SHP'),
            makeFile('UPPER.SHX'),
        ]);

        expect(unusable).toHaveLength(0);
        expect(bundles).toHaveLength(1);
        expect(await membersOf(bundles[0].file)).toEqual(['UPPER.SHP', 'UPPER.SHX']);
    });

    it('explains a shapefile sidecar whose .shp was never selected', async () => {
        const { bundles, passthrough, unusable } = await groupShapefiles([
            makeFile('orphan.prj'),
        ]);

        expect(bundles).toHaveLength(0);
        expect(passthrough).toHaveLength(0);
        expect(unusable.map((u) => u.file.name)).toEqual(['orphan.prj']);
        expect(unusable[0].reason).toContain('orphan.shp');
    });
});

describe('groupShapefiles — MapInfo', () => {
    it('zips a TAB with its .dat/.map/.id instead of uploading the .tab alone', async () => {
        const { bundles, passthrough, unusable } = await groupShapefiles([
            makeFile('Dacite_Domes.TAB'),
            makeFile('Dacite_Domes.DAT'),
            makeFile('Dacite_Domes.MAP'),
            makeFile('Dacite_Domes.ID'),
        ]);

        expect(passthrough).toHaveLength(0);
        expect(unusable).toHaveLength(0);
        expect(bundles).toHaveLength(1);
        expect(bundles[0].kind).toBe('mapinfo');
        expect(bundles[0].file.name).toBe('Dacite_Domes.zip');
        expect(await membersOf(bundles[0].file)).toEqual([
            'Dacite_Domes.DAT',
            'Dacite_Domes.ID',
            'Dacite_Domes.MAP',
            'Dacite_Domes.TAB',
        ]);
        // .ind is an optional attribute index, so it is listed as missing but
        // does not make the set incomplete.
        expect(bundles[0].missing).toEqual(['ind']);
        expect(bundles[0].verdict).toBeNull();
    });

    it('states the verdict on a lone .tab rather than uploading it silently', async () => {
        // Five lone .TAB files in the real delivery. Each one uploads fine and
        // then fails in the worker with a DataSourceError nobody reads.
        const { bundles } = await groupShapefiles([makeFile('Veins.TAB')]);

        expect(bundles).toHaveLength(1);
        expect(bundles[0].missing).toEqual(['dat', 'map', 'id', 'ind']);
        expect(bundles[0].verdict).toContain('Incomplete MapInfo TAB set');
        expect(bundles[0].verdict).toContain('.map');
    });

    it('pairs a .mif with its .mid and flags a .mif that has none', async () => {
        const paired = await groupShapefiles([
            makeFile('alteration.mif'),
            makeFile('alteration.mid'),
        ]);
        expect(paired.bundles).toHaveLength(1);
        expect(await membersOf(paired.bundles[0].file)).toEqual([
            'alteration.mid',
            'alteration.mif',
        ]);
        expect(paired.bundles[0].verdict).toBeNull();

        // A .mif with no .mid parses and returns every attribute as null —
        // the same silent-degradation class as a .shp with no .dbf.
        const lone = await groupShapefiles([makeFile('alteration.mif')]);
        expect(lone.bundles[0].verdict).toContain('Incomplete MapInfo MIF set');
    });

    it('never treats a .mid as an entry point of its own', async () => {
        // A .mid opens directly in GDAL. If it were bundled as a master, a
        // MIF/MID pair would be ingested twice.
        const { bundles, passthrough, unusable } = await groupShapefiles([
            makeFile('alteration.mid'),
        ]);

        expect(bundles).toHaveLength(0);
        expect(passthrough).toHaveLength(0);
        expect(unusable.map((u) => u.file.name)).toEqual(['alteration.mid']);
    });

    it('keeps orphaned MapInfo members with the reason instead of dropping them', async () => {
        // The real folder holds seven .DAT, three .MAP, an .ID and an .IND
        // whose masters were never delivered. A bundler that answers "no
        // master, therefore drop" loses every one of them without a word.
        const { bundles, passthrough, unusable } = await groupShapefiles([
            makeFile('Unga_Geology.DAT'),
            makeFile('Unga_Geology.MAP'),
            makeFile('Shumagin.ID'),
            makeFile('Shumagin.IND'),
        ]);

        expect(bundles).toHaveLength(0);
        expect(passthrough).toHaveLength(0);
        expect(unusable.map((u) => u.file.name).sort()).toEqual([
            'Shumagin.ID',
            'Shumagin.IND',
            'Unga_Geology.DAT',
            'Unga_Geology.MAP',
        ]);
        for (const u of unusable) {
            expect(u.reason, u.file.name).toContain('MapInfo');
        }
    });

    it('produces two bundles when a .shp and a .tab share a stem', async () => {
        const { bundles, passthrough } = await groupShapefiles([
            makeFile('veins.shp'),
            makeFile('veins.prj'),
            makeFile('veins.tab'),
            makeFile('veins.dat'),
            makeFile('veins.map'),
            makeFile('veins.id'),
        ]);

        expect(passthrough).toHaveLength(0);
        expect(bundles.map((b) => b.kind)).toEqual(['shapefile', 'mapinfo']);
        expect(await membersOf(bundles[0].file)).toEqual(['veins.prj', 'veins.shp']);
        expect(await membersOf(bundles[1].file)).toEqual([
            'veins.dat',
            'veins.id',
            'veins.map',
            'veins.tab',
        ]);
    });
});

describe('isShapefileSidecar', () => {
    it.each(['shx', 'dbf', 'prj', 'cpg', 'SHX'])('recognises .%s', (ext) => {
        expect(isShapefileSidecar(ext)).toBe(true);
    });

    it.each(['shp', 'pdf', 'csv', 'zip'])('does not claim .%s', (ext) => {
        expect(isShapefileSidecar(ext)).toBe(false);
    });
});

describe('isMapInfoSidecar', () => {
    it.each(['dat', 'map', 'id', 'ind', 'mid', 'MID'])('recognises .%s', (ext) => {
        expect(isMapInfoSidecar(ext)).toBe(true);
    });

    it.each(['tab', 'mif', 'shp', 'csv'])('does not claim .%s', (ext) => {
        expect(isMapInfoSidecar(ext)).toBe(false);
    });
});

describe('BUNDLE_MEMBER_EXTS', () => {
    it('covers every sidecar an accept= attribute has to let through', () => {
        // The root cause of the .prj-less bundles: the wizard's file picker
        // was built from the category map alone, and no sidecar has a
        // category, so the OS dialog greyed all of them out.
        for (const ext of ['shx', 'dbf', 'prj', 'cpg', 'dat', 'map', 'id', 'ind', 'mid']) {
            expect(BUNDLE_MEMBER_EXTS, `.${ext}`).toContain(ext);
        }
    });

    it('does not list a master, which has a category of its own', () => {
        for (const ext of ['shp', 'tab', 'mif']) {
            expect(BUNDLE_MEMBER_EXTS, `.${ext}`).not.toContain(ext);
        }
    });
});
