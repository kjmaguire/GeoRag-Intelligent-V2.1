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

/** Read one member back out of a bundle, to prove what was actually written. */
async function memberText(file: File, name: string): Promise<string> {
    const zip = await JSZip.loadAsync(await file.arrayBuffer());
    const entry = zip.file(name);
    if (!entry) throw new Error(`${name} is not in ${file.name}`);
    return await entry.async('string');
}

/**
 * The real WKT from the RedStar delivery — verbatim from
 * `Unga Regional (inc)/Geology/Digital Data/Drobeck_Shumagin_Veins.prj`, which
 * is byte-identical to `Geology/2005/GeoPoints_2005.prj` three folders away.
 * That is EPSG:26904, and it is also what the two raster TABs declare as
 * `CoordSys Earth Projection 8, 74, "m", -159, 0, 0.9996, 500000, 0`.
 */
const UTM4N_WKT =
    'PROJCS["NAD_1983_UTM_Zone_4N",GEOGCS["GCS_North_American_1983",' +
    'DATUM["D_North_American_1983",SPHEROID["GRS_1980",6378137.0,298.257222101]],' +
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],' +
    'PROJECTION["Transverse_Mercator"],PARAMETER["False_Easting",500000.0],' +
    'PARAMETER["False_Northing",0.0],PARAMETER["Central_Meridian",-159.0],' +
    'PARAMETER["Scale_Factor",0.9996],PARAMETER["Latitude_Of_Origin",0.0],' +
    'UNIT["Meter",1.0]]';

/** A second, genuinely different coordinate system. */
const WGS84_WKT =
    'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",' +
    'SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],' +
    'UNIT["Degree",0.0174532925199433]]';

/**
 * A real raster TAB header, trimmed — `BMGC_UngaIsSouth_Geology_1990.TAB`.
 * It has no `.dat`, `.map` or `.id` and never will: the table it describes is
 * a scanned geology map.
 */
const RASTER_TAB_HEADER = [
    '!table',
    '!version 300',
    '!charset WindowsLatin1',
    '',
    'Definition Table',
    '  File "bmgc_ungaissouth_geology_1990.tif"',
    '  Type "RASTER"',
    '  (390521.53703414451,6125433.1572805978) (0,0) Label "Pt 1",',
    '  CoordSys Earth Projection 8, 74, "m", -159, 0, 0.9996, 500000, 0',
    '  Units "m"',
    '',
].join('\n');

/** A real NATIVE TAB header — `Sitka_trA.tab`. Unquoted `Type NATIVE`. */
const NATIVE_TAB_HEADER = [
    '!table',
    '!version 300',
    '!charset WindowsLatin1',
    '',
    'Definition Table',
    '  Type NATIVE Charset "WindowsLatin1"',
    '  Fields 3',
    '    ID Integer ;',
    '    NumVal Float ;',
    '    StrVal Char (50) ;',
    '',
].join('\n');

/**
 * A real Discover ground-control-point header — `tr006.4-geology_gcp.TAB`.
 * `Type NATIVE`, so the RASTER check does not see it, but its columns are the
 * warp schema and it carries the delivery's coordinate system in metadata.
 */
const GCP_TAB_HEADER = [
    '!table',
    '!version 300',
    'Definition Table',
    '  Type NATIVE Charset "Neutral"',
    '  Fields 10',
    '    ID Integer ;',
    '    Image_X Integer ;',
    '    Image_Y Integer ;',
    '    Map_X Float ;',
    '    Map_Y Float ;',
    '    RMS Float ;',
    'begin_metadata',
    '"\\Discover\\Warp" = ""',
    '"\\Discover\\Warp\\ProjectionName" = "UTM Zone 4 (NAD 83)"',
    'end_metadata',
    '',
].join('\n');

/** A real Discover cross-section header — `Sitka_trA.tab`, metadata included. */
const XSECT_TAB_HEADER = [
    '!table',
    'Definition Table',
    '  Type NATIVE Charset "WindowsLatin1"',
    '  Fields 3',
    '    ID Integer ;',
    '    NumVal Float ;',
    '    StrVal Char (50) ;',
    'begin_metadata',
    '"\\Discover\\xsects" = ""',
    '"\\Discover\\xsects\\project" = "Sitka_tr"',
    'end_metadata',
    '',
].join('\n');

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

describe('groupShapefiles - CRS donation', () => {
    it("copies the selection's only .prj into every shapefile that has none", async () => {
        // The measured RedStar case: 33 GIS stems, zero complete sets, seven
        // shapefiles missing only their .prj - and the coordinate system
        // sitting in the same delivery, declared identically in three places.
        // Asking the user to type EPSG:26904 seven times is asking them to
        // re-key information they already handed over.
        const { bundles, crsDonation } = await groupShapefiles([
            makeFile('Drobeck_Shumagin_Veins.shp'),
            makeFile('Drobeck_Shumagin_Veins.prj', '', UTM4N_WKT),
            makeFile('geology_poly.shp'),
            makeFile('geology_poly.dbf'),
            makeFile('alteration.shp'),
        ]);

        expect(crsDonation).not.toBeNull();
        expect(crsDonation?.sourceName).toBe('Drobeck_Shumagin_Veins.prj');
        expect(crsDonation?.wkt).toBe(UTM4N_WKT);
        expect(crsDonation?.label).toBe('NAD 1983 UTM Zone 4N');
        expect(crsDonation?.appliedTo).toEqual(['geology_poly', 'alteration']);

        // Identity travels on the bundle, not in a stem list. `memberName` is
        // the exact entry that was added, so declining the donation removes
        // that entry and not a name rebuilt from the stem.
        expect(bundles[0].crsFrom).toBeNull();
        expect(bundles[1].crsFrom).toEqual({
            sourceName: 'Drobeck_Shumagin_Veins.prj',
            label: 'NAD 1983 UTM Zone 4N',
            memberName: 'geology_poly.prj',
        });
        expect(bundles[2].crsFrom?.memberName).toBe('alteration.prj');

        // The bytes really are in the ZIP, under the recipient's own stem -
        // that is the whole mechanism. Nothing resolves WKT to an EPSG code
        // in the browser; pyproj reads this copy exactly as it reads a .prj
        // the file came with.
        expect(await membersOf(bundles[1].file)).toEqual([
            'geology_poly.dbf',
            'geology_poly.prj',
            'geology_poly.shp',
        ]);
        expect(await memberText(bundles[1].file, 'geology_poly.prj')).toBe(UTM4N_WKT);
        expect(bundles[1].members).toContain('geology_poly.prj');
        expect(bundles[1].missing).toEqual(['shx']);
        expect(bundles[1].verdict).toContain('copied from Drobeck_Shumagin_Veins.prj');
        expect(bundles[1].verdict).toContain('NAD 1983 UTM Zone 4N');
        expect(bundles[1].verdict).not.toContain('set an EPSG code');

        // A donated CRS does not paper over the other half of an incomplete
        // set: alteration.shp still arrives with no attributes.
        expect(bundles[2].missing).toEqual(['shx', 'dbf']);
        expect(bundles[2].verdict).toContain('no .dbf');
        expect(bundles[2].verdict).toContain('copied from Drobeck_Shumagin_Veins.prj');

        // The donor keeps its own file and is not listed as a recipient.
        expect(await membersOf(bundles[0].file)).toEqual([
            'Drobeck_Shumagin_Veins.prj',
            'Drobeck_Shumagin_Veins.shp',
        ]);
        expect(crsDonation?.appliedTo).not.toContain('Drobeck_Shumagin_Veins');
    });

    it('donates nothing when the selection holds two different coordinate systems', async () => {
        // The dangerous case. Spreading one of two coordinate systems across
        // a delivery is the same class of silent corruption as assuming 4326
        // for a CRS-less file, which is the bug that started all of this.
        const { bundles, crsDonation } = await groupShapefiles([
            makeFile('a.shp'),
            makeFile('a.prj', '', UTM4N_WKT),
            makeFile('b.shp'),
            makeFile('b.prj', '', WGS84_WKT),
            makeFile('c.shp'),
        ]);

        expect(crsDonation).toBeNull();
        expect(await membersOf(bundles[2].file)).toEqual(['c.shp']);
        expect(bundles[2].missing).toContain('prj');
        expect(bundles[2].verdict).toContain('set an EPSG code');
        for (const b of bundles) {
            expect(b.crsFrom, b.stem).toBeNull();
        }
    });

    it('lets a .qpj declaring a different CRS suppress the donation entirely', async () => {
        // QGIS wrote its own copy of the WKT into a .qpj for years, and it can
        // disagree with the .prj beside it. Reading only .prj files made this
        // selection look unanimous, and c.shp would have been handed UTM 4N
        // while b.shp sat there declaring WGS84 — the two-coordinate-systems
        // case, arrived at by not looking.
        const { bundles, crsDonation } = await groupShapefiles([
            makeFile('a.shp'),
            makeFile('a.prj', '', UTM4N_WKT),
            makeFile('b.shp'),
            makeFile('b.qpj', '', WGS84_WKT),
            makeFile('c.shp'),
        ]);

        expect(crsDonation).toBeNull();
        for (const b of bundles) {
            expect(b.crsFrom, b.stem).toBeNull();
        }
        expect(await membersOf(bundles[2].file)).toEqual(['c.shp']);
        expect(bundles[2].verdict).toContain('set an EPSG code');
    });

    it('does not donate into a shapefile that declares its CRS in a .qpj', async () => {
        // Every WKT here agrees, so the donation runs — but qgis_layer already
        // says what it is in its own .qpj. Writing another file's WKT in beside
        // that is the contradiction the whole mechanism exists to avoid, and
        // "it happens to match this time" is not a reason to start doing it.
        const { bundles, crsDonation } = await groupShapefiles([
            makeFile('Drobeck.shp'),
            makeFile('Drobeck.prj', '', UTM4N_WKT),
            makeFile('qgis_layer.shp'),
            makeFile('qgis_layer.qpj', '', UTM4N_WKT),
            makeFile('bare.shp'),
        ]);

        // A .prj is preferred as the named source over a .qpj carrying the
        // same bytes: it is the file the user will recognise.
        expect(crsDonation?.sourceName).toBe('Drobeck.prj');
        expect(crsDonation?.appliedTo).toEqual(['bare']);

        expect(bundles[1].stem).toBe('qgis_layer');
        expect(bundles[1].crsFrom).toBeNull();
        expect(await membersOf(bundles[1].file)).toEqual([
            'qgis_layer.qpj',
            'qgis_layer.shp',
        ]);
        // The .prj is still absent and the ingest still needs one, so the row
        // says so — but not by claiming the file declares no coordinate
        // system, which would be false with the .qpj sitting in the ZIP.
        expect(bundles[1].missing).toContain('prj');
        expect(bundles[1].verdict).toContain('.qpj');
        expect(bundles[1].verdict).toContain('EPSG');
        expect(bundles[1].verdict).not.toContain('copied from');
        expect(bundles[1].verdict).not.toContain('declares no coordinate system');

        expect(bundles[2].crsFrom?.memberName).toBe('bare.prj');
    });

    it('treats byte-identical and whitespace-differing copies as one WKT', async () => {
        // Three .prj files, one coordinate system. A trailing CRLF from one
        // toolchain must not read as a second CRS and suppress the donation
        // on exactly the delivery that needs it.
        const { bundles, crsDonation } = await groupShapefiles([
            makeFile('a.shp'),
            makeFile('a.prj', '', UTM4N_WKT),
            makeFile('b.shp'),
            makeFile('b.prj', '', UTM4N_WKT),
            makeFile('c.shp'),
            makeFile('c.prj', '', `${UTM4N_WKT}\r\n`),
            makeFile('d.shp'),
        ]);

        expect(crsDonation?.sourceName).toBe('a.prj');
        expect(crsDonation?.appliedTo).toEqual(['d']);
        expect(await memberText(bundles[3].file, 'd.prj')).toBe(UTM4N_WKT);
        expect(bundles[3].crsFrom?.memberName).toBe('d.prj');
    });

    it('marks the recipient on the bundle, not by stem, when two folders share one', async () => {
        // THE case stem-keying gets wrong. groupKey deliberately keeps
        // geology/faults.shp and claims/faults.shp apart — the real delivery
        // has same-stem layers in different folders — so both bundles come
        // back with stem "faults" and only ONE of them took a donation.
        // Anything matching recipients by stem attributes the donation to both
        // and, when the user declines it, tries to strip a .prj out of the
        // archive that never received one.
        const { bundles, crsDonation } = await groupShapefiles([
            makeFile('faults.shp', 'geology'),
            makeFile('faults.shx', 'geology'),
            makeFile('faults.dbf', 'geology'),
            makeFile('faults.prj', 'geology', UTM4N_WKT),
            makeFile('faults.shp', 'claims'),
            makeFile('faults.shx', 'claims'),
        ]);

        expect(bundles).toHaveLength(2);
        expect(bundles[0].stem).toBe('faults');
        expect(bundles[1].stem).toBe('faults');
        expect(crsDonation?.appliedTo).toEqual(['faults']);

        // geology/faults already had its own .prj: complete set, no donation.
        expect(bundles[0].crsFrom).toBeNull();
        expect(bundles[0].verdict).toBeNull();
        expect(await membersOf(bundles[0].file)).toEqual([
            'faults.dbf',
            'faults.prj',
            'faults.shp',
            'faults.shx',
        ]);

        // claims/faults took the copy — and it is still missing its .dbf, so
        // the donation must not read as "this set is now complete".
        expect(bundles[1].crsFrom).toEqual({
            sourceName: 'faults.prj',
            label: 'NAD 1983 UTM Zone 4N',
            memberName: 'faults.prj',
        });
        expect(await membersOf(bundles[1].file)).toEqual([
            'faults.prj',
            'faults.shp',
            'faults.shx',
        ]);
        expect(await memberText(bundles[1].file, 'faults.prj')).toBe(UTM4N_WKT);
        expect(bundles[1].missing).toEqual(['dbf']);
        expect(bundles[1].verdict).toContain('no .dbf');
        expect(bundles[1].verdict).toContain('copied from faults.prj');
    });

    it('stops calling an orphaned .prj unusable once its WKT has been donated', async () => {
        // GeoPoints_2005.prj is an orphan in the real delivery - its .shp was
        // never sent - and it carries the same WKT as everything else. Before
        // this it was reported as a file with nothing to attach to, moments
        // before the same bytes would have rescued seven other uploads.
        const { bundles, unusable, crsDonation } = await groupShapefiles([
            makeFile('geology_poly.shp'),
            makeFile('GeoPoints_2005.prj', '', UTM4N_WKT),
        ]);

        expect(unusable).toHaveLength(0);
        expect(crsDonation?.sourceName).toBe('GeoPoints_2005.prj');
        expect(crsDonation?.appliedTo).toEqual(['geology_poly']);
        expect(bundles[0].crsFrom?.sourceName).toBe('GeoPoints_2005.prj');
        expect(await membersOf(bundles[0].file)).toEqual([
            'geology_poly.prj',
            'geology_poly.shp',
        ]);
    });

    it('still reports an orphaned .prj that nothing could take', async () => {
        // No recipient, no donation. A "coordinate system applied" line on a
        // screen where nothing was applied to anything is worse than silence.
        const { unusable, crsDonation } = await groupShapefiles([
            makeFile('orphan.prj', '', UTM4N_WKT),
        ]);

        expect(crsDonation).toBeNull();
        expect(unusable.map((u) => u.file.name)).toEqual(['orphan.prj']);
    });

    it('does not donate into a MapInfo bundle, which reads no .prj', async () => {
        // A TAB's coordinate system lives in its .map. Writing a .prj beside
        // it would be theatre: GDAL never looks at one.
        const { bundles, unusable, crsDonation } = await groupShapefiles([
            makeFile('Veins.TAB', '', NATIVE_TAB_HEADER),
            makeFile('Veins.DAT'),
            makeFile('Veins.MAP'),
            makeFile('Veins.ID'),
            makeFile('GeoPoints_2005.prj', '', UTM4N_WKT),
        ]);

        expect(crsDonation).toBeNull();
        expect(bundles[0].crsFrom).toBeNull();
        expect(await membersOf(bundles[0].file)).toEqual([
            'Veins.DAT',
            'Veins.ID',
            'Veins.MAP',
            'Veins.TAB',
        ]);
        expect(unusable.map((u) => u.file.name)).toEqual(['GeoPoints_2005.prj']);
    });

    it('labels a geographic CRS from its GEOGCS name', async () => {
        const { crsDonation } = await groupShapefiles([
            makeFile('pts.shp'),
            makeFile('donor.prj', '', WGS84_WKT),
        ]);

        expect(crsDonation?.label).toBe('GCS WGS 1984');
    });

    it('reads a WKT2 PROJCRS name as well as a WKT1 PROJCS one', async () => {
        const { crsDonation } = await groupShapefiles([
            makeFile('pts.shp'),
            makeFile(
                'donor.prj',
                '',
                'PROJCRS["NAD83 / UTM zone 4N",BASEGEOGCRS["NAD83",' +
                    'DATUM["North American Datum 1983"]],CONVERSION["UTM zone 4N"]]',
            ),
        ]);

        expect(crsDonation?.label).toBe('NAD83 / UTM zone 4N');
    });

    it.each([
        ['an empty .prj', ''],
        ['a .prj of non-WKT junk', 'EPSG:26904\n'],
        ['a .prj naming no CRS type we recognise', 'LOCAL_CS["mine grid"]'],
    ])('donates nothing from %s', async (_label, body) => {
        // "The selection agrees on one WKT" is satisfied by a single empty
        // file — a set of size one — and crsLabel then falls back to the file
        // NAME, so the screen would have offered "donor.prj" as the coordinate
        // system being applied to every CRS-less shapefile in the drop. A
        // donor has to actually name a CRS.
        const { bundles, unusable, crsDonation } = await groupShapefiles([
            makeFile('pts.shp'),
            makeFile('donor.prj', '', body),
        ]);

        expect(crsDonation).toBeNull();
        expect(bundles[0].crsFrom).toBeNull();
        expect(await membersOf(bundles[0].file)).toEqual(['pts.shp']);
        // The recipient keeps the ordinary verdict: prompt for an EPSG code,
        // exactly as if no .prj had been dropped at all.
        expect(bundles[0].missing).toContain('prj');
        expect(bundles[0].verdict).toContain('set an EPSG code');
        // And nothing was donated, so the orphan is still reported.
        expect(unusable.map((u) => u.file.name)).toEqual(['donor.prj']);
    });

    it('falls back to the file name when a CRS keyword carries no quoted name', async () => {
        // The label is for a human to sanity-check. It is not a CRS
        // identifier, and nothing downstream parses it.
        const { crsDonation } = await groupShapefiles([
            makeFile('pts.shp'),
            makeFile('donor.prj', '', 'PROJCS[Unquoted_Mine_Grid,UNIT[Meter,1.0]]'),
        ]);

        expect(crsDonation?.label).toBe('donor.prj');
    });
});

describe('groupShapefiles - raster TAB', () => {
    it('names the image a raster TAB georeferences instead of demanding a .dat', async () => {
        // Two of the five .TAB files in the delivery are Type "RASTER": they
        // are the georeferencing for a scanned geology map. They have no
        // .dat/.map/.id and never will, so "add the missing files and drop
        // the folder again" is advice that cannot be followed.
        const { bundles, passthrough, unusable } = await groupShapefiles([
            makeFile('BMGC_UngaIsSouth_Geology_1990.TAB', '', RASTER_TAB_HEADER),
        ]);

        expect(bundles).toHaveLength(0);
        expect(passthrough).toHaveLength(0);
        expect(unusable.map((u) => u.file.name)).toEqual([
            'BMGC_UngaIsSouth_Geology_1990.TAB',
        ]);

        const reason = unusable[0].reason;
        expect(reason).toContain('bmgc_ungaissouth_geology_1990.tif');
        expect(reason).toContain('not a vector table');
        expect(reason).toContain('was not selected');
        expect(reason).not.toContain('.dat');
        expect(reason).not.toContain('Incomplete');
    });

    it('neither repeats the file name nor promises the CRS will be applied', async () => {
        // Two separate wrongs in one sentence. Both screens render an unusable
        // row as `${u.file.name}: ${u.reason}`, so a reason opening with the
        // name printed it twice — and the tail said "upload the image itself
        // and this file's coordinate system will be applied", which nothing
        // does: the .tab is reported here and never uploaded, and no code
        // reads its CoordSys line. Unfollowable advice inside the feature
        // built to stop giving unfollowable advice.
        const { unusable } = await groupShapefiles([
            makeFile('BMGC_UngaIsSouth_Geology_1990.TAB', '', RASTER_TAB_HEADER),
        ]);

        const reason = unusable[0].reason;
        expect(reason.startsWith('BMGC_UngaIsSouth_Geology_1990.TAB')).toBe(false);
        expect(reason).not.toContain('will be applied');
        expect(reason).toContain('not read yet');
        // What the row renders as, end to end.
        expect(`${unusable[0].file.name}: ${reason}`).toBe(
            'BMGC_UngaIsSouth_Geology_1990.TAB: Georeferencing for ' +
                'bmgc_ungaissouth_geology_1990.tif, not a vector table. ' +
                'bmgc_ungaissouth_geology_1990.tif was not selected - add it and drop the ' +
                'folder again to upload it. The coordinate system in this file is not read yet.',
        );
    });

    it('says so when the image a raster TAB points at is in the selection', async () => {
        // The header spells the image in lower case and the file on disk does
        // not; matching case-sensitively would report the image as absent
        // while it sat in the same drop.
        const { bundles, passthrough, unusable } = await groupShapefiles([
            makeFile('BMGC_UngaIsSouth_Geology_1990.TAB', '', RASTER_TAB_HEADER),
            makeFile('BMGC_UngaIsSouth_Geology_1990.tif'),
        ]);

        expect(bundles).toHaveLength(0);
        expect(passthrough.map((f) => f.name)).toEqual([
            'BMGC_UngaIsSouth_Geology_1990.tif',
        ]);
        expect(unusable[0].reason).toContain('is in this selection');
    });

    it('leaves a NATIVE TAB missing its .map on exactly the old verdict', async () => {
        // Unquoted `Type NATIVE`, and genuinely dead without the .map: this
        // message was already right and must not drift.
        const { bundles, unusable } = await groupShapefiles([
            makeFile('Sitka_trA.tab', '', NATIVE_TAB_HEADER),
            makeFile('Sitka_trA.dat'),
            makeFile('Sitka_trA.id'),
        ]);

        expect(unusable).toHaveLength(0);
        expect(bundles).toHaveLength(1);
        expect(bundles[0].kind).toBe('mapinfo');
        expect(bundles[0].missing).toEqual(['map', 'ind']);
        expect(bundles[0].verdict).toBe(
            'Incomplete MapInfo TAB set: no .map. A .tab holds neither the geometry nor the ' +
                'coordinate system - both live in the .map - so GDAL cannot open this one. Add ' +
                'the missing files and drop the folder again.',
        );
    });
});

describe('groupShapefiles - NATIVE TABs that are not map layers', () => {
    it('names a ground-control-point table instead of demanding its sidecars', async () => {
        // `*_gcp.TAB` is the warp table for a scanned trench map: pixel
        // positions and their map coordinates. It is Type NATIVE, so it used
        // to fall through to the sidecar check and be reported as
        // "Incomplete MapInfo TAB set: no .dat, .map, .id" — files that were
        // never part of it, and that would not make it vector data.
        const { bundles, passthrough, unusable } = await groupShapefiles([
            makeFile('tr006.4-geology_gcp.TAB', '', GCP_TAB_HEADER),
        ]);

        expect(bundles).toHaveLength(0);
        expect(passthrough).toHaveLength(0);
        expect(unusable).toHaveLength(1);

        const reason = unusable[0].reason;
        expect(reason).toContain('control-point table');
        expect(reason).not.toContain('Incomplete');
        expect(reason).not.toContain('drop the folder again');
    });

    it('reports the coordinate system a GCP header declares', async () => {
        // The one genuinely useful thing in the file: these headers name the
        // CRS the .prj-less shapefiles in the same delivery needed.
        const { unusable } = await groupShapefiles([
            makeFile('tr006.4-geology_gcp.TAB', '', GCP_TAB_HEADER),
        ]);

        expect(unusable[0].reason).toContain('UTM Zone 4 (NAD 83)');
    });

    it('names a Discover cross-section table for what it is', async () => {
        const { unusable } = await groupShapefiles([
            makeFile('Sitka_trA.tab', '', XSECT_TAB_HEADER),
        ]);

        expect(unusable).toHaveLength(1);
        const reason = unusable[0].reason;
        expect(reason).toContain('cross-section definition');
        expect(reason).toContain('collar and interval tables');
        expect(reason).not.toContain('Incomplete');
    });

    it('does not repeat the file name, which the row already renders', async () => {
        const { unusable } = await groupShapefiles([
            makeFile('tr006.4-geology_gcp.TAB', '', GCP_TAB_HEADER),
        ]);

        expect(unusable[0].reason.startsWith('tr006.4-geology_gcp.TAB')).toBe(false);
    });

    it('still asks for sidecars when the NATIVE table really is a map layer', async () => {
        // The regression guard for the two branches above: an ordinary NATIVE
        // .tab with no Discover metadata is a vector table missing its
        // sidecars, and that advice IS followable.
        //
        // An incomplete set stays a BUNDLE carrying a verdict — it is still
        // uploaded, and the verdict is what the "Files needing attention"
        // list renders. Only a file with nothing worth uploading becomes
        // `unusable`, which is where the two Discover kinds above now go.
        const { bundles, unusable } = await groupShapefiles([
            makeFile('Veins.TAB', '', NATIVE_TAB_HEADER),
        ]);

        expect(unusable).toHaveLength(0);
        expect(bundles).toHaveLength(1);
        expect(bundles[0].verdict).toContain('Incomplete MapInfo TAB set');
    });
});

describe('groupShapefiles — WKT-carriage donation (.dxf/.dgn)', () => {
    // The RedStar shape of the problem: the same GeoPoints_2005.prj that
    // rescued seven .prj-less shapefiles named the CRS the lone DXF needed,
    // and nothing could carry it there — a .dxf is one file, not a ZIP a
    // copy could be zipped into. It takes the donation as text instead
    // (uploaded as `source_crs_wkt`; the server resolves it with pyproj).

    it('hands a lone .dxf the agreed WKT as text, not as a member', async () => {
        const dxf = makeFile('NEW_HYD.BX_Central_Clean.dxf');
        const { passthrough, wktRecipients, crsDonation, unusable } =
            await groupShapefiles([
                dxf,
                makeFile('GeoPoints_2005.prj', '', UTM4N_WKT),
            ]);

        // The file itself still goes up byte-identical, as itself.
        expect(passthrough).toContain(dxf);
        expect(wktRecipients).toHaveLength(1);
        expect(wktRecipients[0].file).toBe(dxf);
        expect(wktRecipients[0].crs.sourceName).toBe('GeoPoints_2005.prj');
        expect(wktRecipients[0].crs.label).toBe('NAD 1983 UTM Zone 4N');
        expect(wktRecipients[0].crs.wkt).toBe(UTM4N_WKT);
        // Text carriage, never a ZIP entry: memberName gates the
        // "rebuild the archive without the copy" path and must be absent.
        expect(wktRecipients[0].crs.memberName).toBeUndefined();
        // The recipient makes the donation real, so the banner exists and
        // the orphaned donor is no longer reported as unusable.
        expect(crsDonation?.appliedTo).toContain('NEW_HYD.BX_Central_Clean');
        expect(unusable).toHaveLength(0);
    });

    it('serves a bundle and a .dxf from the same donor', async () => {
        const { bundles, wktRecipients, crsDonation } = await groupShapefiles([
            makeFile('veins.shp'),
            makeFile('veins.shx'),
            makeFile('plan.dxf'),
            makeFile('GeoPoints_2005.prj', '', UTM4N_WKT),
        ]);

        expect(bundles).toHaveLength(1);
        expect(bundles[0].crsFrom?.memberName).toBe('veins.prj');
        expect(wktRecipients).toHaveLength(1);
        expect(crsDonation?.appliedTo).toEqual(
            expect.arrayContaining(['veins', 'plan']),
        );
    });

    it('a .dgn takes the donation the same way', async () => {
        const { wktRecipients } = await groupShapefiles([
            makeFile('site.dgn'),
            makeFile('donor.prj', '', UTM4N_WKT),
        ]);

        expect(wktRecipients).toHaveLength(1);
        expect(wktRecipients[0].crs.wkt).toBe(UTM4N_WKT);
    });

    it('two distinct coordinate systems donate to nothing, .dxf included', async () => {
        const { wktRecipients, crsDonation } = await groupShapefiles([
            makeFile('plan.dxf'),
            makeFile('a.prj', '', UTM4N_WKT),
            makeFile('b.prj', '', WGS84_WKT),
        ]);

        expect(wktRecipients).toHaveLength(0);
        expect(crsDonation).toBeNull();
    });

    it('GeoJSON is never a recipient — RFC 7946 is a declaration', async () => {
        const { wktRecipients } = await groupShapefiles([
            makeFile('sites.geojson'),
            makeFile('donor.prj', '', UTM4N_WKT),
        ]);

        expect(wktRecipients).toHaveLength(0);
    });

    it('no donor, no recipients', async () => {
        const { wktRecipients } = await groupShapefiles([makeFile('plan.dxf')]);

        expect(wktRecipients).toHaveLength(0);
    });
});
