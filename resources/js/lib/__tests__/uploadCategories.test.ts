import { describe, expect, it } from 'vitest';

// Vite's `?raw` import, matching the pattern in
// Components/__tests__/MapView.auth.test.ts. Reading the file through the
// bundler rather than node:fs keeps this test free of @types/node, which is
// not a declared dependency and is not in tsconfig's `types`.
import uploadControllerSource from '../../../../app/Http/Controllers/Api/V1/UploadController.php?raw';

import {
  CATEGORY_EXTS,
  CATEGORY_LABEL,
  EPSG_MAX,
  EPSG_MIN,
  LIVE_CATEGORIES,
  RETIRED_CATEGORIES,
  acceptedExtensions,
  categoryForExtension,
  extensionOf,
  parseEpsg,
  supportsCrsOverride,
  type Category,
} from '../uploadCategories';

/**
 * These tests read UploadController.php directly.
 *
 * That is deliberate. The frontend's idea of which categories exist had
 * drifted from the backend's in BOTH directions and neither was caught:
 *
 *   - the picker offered `spatial` for .kmz/.kml, which the API has never
 *     accepted, so choosing one produced a 422 after the upload
 *   - the import wizard hardcoded PDF/TIFF/ZIP and refused drill and GIS
 *     files client-side, so they stayed unreachable after the API began
 *     accepting them
 *
 * A test that only checked the TypeScript against itself would have passed
 * throughout. Parsing the PHP is the only thing that actually pins them
 * together.
 */

/**
 * A flat `private const NAME = ['a', 'b'];` in UploadController.
 *
 * Needed because a category list may be BUILT from one rather than spelled
 * out — `'reports' => ['pdf', ...self::RASTER_REPORT_EXTS]`. That spread
 * exists so the raster extensions are declared once instead of in the three
 * places the controller's own docblock warns about; without resolving it
 * here, this guard reads `reports` as `['pdf']` and fails on a file that is
 * in fact wired correctly.
 */
function phpListConst(constName: string): string[] {
  const m = new RegExp(`private const ${constName} = \\[([^\\]]*)\\];`).exec(
    uploadControllerSource,
  );
  if (!m) throw new Error(`${constName} not found in UploadController`);
  return [...m[1].matchAll(/'([a-z0-9]+)'/g)].map((x) => x[1]);
}

function phpCategoryBlock(constName: string): Record<string, string[]> {
  const php = uploadControllerSource;
  const block = new RegExp(
    `private const ${constName} = \\[(.*?)\\n    \\];`,
    's',
  ).exec(php);
  if (!block) throw new Error(`${constName} not found in UploadController`);

  const out: Record<string, string[]> = {};
  for (const [, cat, exts] of block[1].matchAll(
    /'([a-z_]+)'\s*=>\s*\[([^\]]*)\]/g,
  )) {
    const literals = [...exts.matchAll(/'([a-z0-9]+)'/g)].map((m) => m[1]);
    // `...self::OTHER_CONST` contributes that constant's members. Resolved
    // rather than ignored: silently dropping it would let the two lists
    // diverge in exactly the direction this file exists to catch.
    const spreads = [...exts.matchAll(/\.\.\.self::([A-Z_]+)/g)].flatMap(
      (m) => phpListConst(m[1]),
    );
    out[cat] = [...literals, ...spreads].sort();
  }
  return out;
}

describe('upload categories match the backend', () => {
  const live = phpCategoryBlock('CATEGORIES');
  const retired = phpCategoryBlock('RETIRED_CATEGORIES');

  it('offers exactly the categories the API accepts', () => {
    expect(LIVE_CATEGORIES.slice().sort()).toEqual(Object.keys(live).sort());
  });

  it('accepts exactly the extensions the API accepts, per category', () => {
    for (const [cat, exts] of Object.entries(live)) {
      expect(
        CATEGORY_EXTS[cat as Category]?.slice().sort(),
        `extensions for '${cat}'`,
      ).toEqual(exts);
    }
  });

  it('never offers a category the API would refuse', () => {
    for (const cat of Object.keys(retired)) {
      expect(
        LIVE_CATEGORIES,
        `'${cat}' is retired in UploadController but offered in the UI`,
      ).not.toContain(cat as Category);
    }
  });

  it('labels every category it can offer', () => {
    for (const cat of LIVE_CATEGORIES) {
      expect(CATEGORY_LABEL[cat], `label for '${cat}'`).toBeTruthy();
    }
  });
});

describe('categoryForExtension', () => {
  it('routes the geology formats to a live category', () => {
    expect(categoryForExtension('pdf')).toBe('reports');
    expect(categoryForExtension('tiff')).toBe('reports');
    expect(categoryForExtension('xlsx')).toBe('excel');
    expect(categoryForExtension('shp')).toBe('spatial');
    expect(categoryForExtension('gpkg')).toBe('spatial');
    expect(categoryForExtension('qgz')).toBe('spatial');
    expect(categoryForExtension('las')).toBe('well_logs');
  });

  it('routes MapInfo entry points to spatial, and geometry-only sidecars nowhere', () => {
    // .tab and .mif are what GDAL opens. .map/.id/.ind carry geometry and
    // index and are meaningless without their master, so they must NOT
    // resolve to a category. .mid is excluded for a different reason: it
    // opens directly, so accepting it would ingest a MIF/MID pair twice.
    expect(categoryForExtension('tab')).toBe('spatial');
    expect(categoryForExtension('mif')).toBe('spatial');
    for (const ext of ['map', 'id', 'ind', 'mid']) {
      expect(categoryForExtension(ext), `'${ext}' resolved to a category`).toBeNull();
    }
  });

  it('routes a MapInfo .dat to the tabular category, exactly like a .dbf', () => {
    // Changed 2026-08-25. A MapInfo .dat IS a dBASE file and reads standalone
    // once its master is absent — the same case as .dbf, which has resolved
    // to `tables` since the standalone-attribute-table work landed.
    //
    // The old expectation (null) rested on a comment claiming .dat was
    // "claimed by the retired xyz category". That constraint does not exist:
    // UploadController consults RETIRED_CATEGORIES by category NAME, never by
    // extension. `txt` sits in retired `xyz` AND in live `collars` and
    // uploads fine — the standing proof.
    //
    // Measured cost of the old behaviour on one delivery: Sitka_trD.DAT
    // (5 trench collars with azimuths, depths and UTM coordinates) and
    // all_historical_soils_clean.DAT (854 soil samples with easting/northing
    // and Au/Ag/As assays) were both discarded as orphaned sidecars.
    expect(categoryForExtension('dat')).toBe('tables');
  });

  it('routes a standalone .dbf to the tabular category, not to spatial', () => {
    // A .dbf beside its .shp never reaches this function — groupShapefiles
    // zips it into the bundle first. One arriving here has no .shp, so it is
    // an attribute table, and ingest_spatial would die on it with
    // "'DataFrame' object has no attribute 'crs'".
    expect(categoryForExtension('dbf')).toBe('tables');
  });

  it('never returns a retired category', () => {
    // .sgy and .xyz map to retired categories, so they must resolve to null
    // rather than to a category the upload would be refused for.
    expect(categoryForExtension('sgy')).toBeNull();
    expect(categoryForExtension('segy')).toBeNull();
    expect(categoryForExtension('xyz')).toBeNull();

    for (const ext of ['csv', 'pdf', 'shp', 'las', 'zip', 'xlsx']) {
      const cat = categoryForExtension(ext);
      expect(cat, `'${ext}' resolved to a category`).not.toBeNull();
      expect(RETIRED_CATEGORIES.has(cat as Category)).toBe(false);
    }
  });

  it('prefers archive over spatial for a bare .zip', () => {
    // Both accept .zip. A mixed bundle of reports is the commoner upload,
    // and the picker lets the user override to `spatial` for a shapefile.
    expect(categoryForExtension('zip')).toBe('archive');
  });

  it('refuses raster images outright', () => {
    for (const ext of ['jpg', 'png', 'gif', 'bmp']) {
      expect(categoryForExtension(ext), `'${ext}'`).toBeNull();
    }
  });

  it('does not treat TIFF as an unsupported image', () => {
    // TIFF scans route through `reports` and normalise to PDF (ADR-0005).
    expect(categoryForExtension('tif')).toBe('reports');
  });

  it('returns null for something nobody accepts', () => {
    expect(categoryForExtension('exe')).toBeNull();
    expect(categoryForExtension('')).toBeNull();
  });
});

describe('extensionOf', () => {
  it('lowercases and takes the last segment', () => {
    expect(extensionOf('Collars.CSV')).toBe('csv');
    expect(extensionOf('a.b.c.gpkg')).toBe('gpkg');
  });

  it('handles a name with no extension', () => {
    expect(extensionOf('README')).toBe('readme');
  });
});

describe('acceptedExtensions', () => {
  it('lists only extensions of live categories', () => {
    const accepted = acceptedExtensions();
    expect(accepted).toContain('shp');
    expect(accepted).toContain('las');
    expect(accepted).toContain('qgz');
    expect(accepted).toContain('tab');
    expect(accepted).toContain('mif');
    expect(accepted).toContain('dbf');
    // .sgy belongs only to a retired category.
    expect(accepted).not.toContain('sgy');
  });

  it('does not offer geometry-only bundle members as standalone uploads', () => {
    // These reach the server inside a ZIP or not at all. Offering one as its
    // own upload gets a 422 at the door, which is how the drop zone taught
    // people to delete their sidecars before importing.
    //
    // `.dat` left this list on 2026-08-25 — see the companion test below.
    // The distinction is whether the file OPENS ALONE: a .dat and a .dbf are
    // whole dBASE tables, while a .shx/.prj/.map/.id is a fragment of another
    // file and means nothing without it.
    const accepted = acceptedExtensions();
    for (const ext of ['shx', 'prj', 'cpg', 'map', 'id', 'ind', 'mid']) {
      expect(accepted, `.${ext}`).not.toContain(ext);
    }
  });

  it('offers the bundle members that are whole tables on their own', () => {
    // Both are dBASE files that open standalone. They are ALSO bundle members
    // — a .dbf beside its .shp, a .dat beside its .tab — and groupShapefiles
    // claims those before this list is ever consulted, so being here does not
    // pull a sidecar out of a complete set.
    const accepted = acceptedExtensions();
    expect(accepted).toContain('dbf');
    expect(accepted).toContain('dat');
  });
});

describe('parseEpsg', () => {
  it('accepts a bare EPSG number inside the range the database enforces', () => {
    expect(parseEpsg('26904')).toEqual({ epsg: 26904 });
    expect(parseEpsg('  32613 ')).toEqual({ epsg: 32613 });
    expect(parseEpsg(String(EPSG_MIN))).toEqual({ epsg: EPSG_MIN });
    expect(parseEpsg(String(EPSG_MAX))).toEqual({ epsg: EPSG_MAX });
  });

  it('treats empty as no override rather than as an error', () => {
    // The override is optional and a file that declares its own CRS wins
    // over it anyway, so blank must not block the upload.
    expect(parseEpsg('')).toEqual({});
    expect(parseEpsg('   ')).toEqual({});
  });

  it('refuses a CRS string, which is the wrong wire type', () => {
    // silver.spatial_features.crs_epsg_native is an integer with a CHECK
    // constraint. Passing 'EPSG:26904' through would be a second spelling of
    // one concept across two ingest paths that share this screen.
    expect(parseEpsg('EPSG:26904').error).toBeTruthy();
    expect(parseEpsg('WGS84').error).toBeTruthy();
    expect(parseEpsg('-26904').error).toBeTruthy();
    expect(parseEpsg('269.04').error).toBeTruthy();
  });

  it('refuses codes outside 1024-32767', () => {
    expect(parseEpsg('4').error).toContain(`${EPSG_MIN}-${EPSG_MAX}`);
    expect(parseEpsg('1023').error).toBeTruthy();
    expect(parseEpsg('32768').error).toBeTruthy();
    expect(parseEpsg('999999').error).toBeTruthy();
  });
});

describe('supportsCrsOverride', () => {
  it('offers the override on every category routed to spatial or tabular ingest', () => {
    // Exactly the categories UploadController::GEOLOGY_WORKFLOWS maps to
    // ingest_spatial or ingest_tabular — the two workflows whose input model
    // declares source_epsg, and the two dispatchGeologyIngest() forwards it to.
    for (const cat of [
      'spatial',
      'collars',
      'surveys',
      'lithology',
      'samples',
      'excel',
      'tables',
    ] as Category[]) {
      expect(supportsCrsOverride(cat), cat).toBe(true);
    }
  });

  it('withholds it where the value would be dropped in transit', () => {
    // reports -> ingest_pdf/tiff_normalize, archive -> ingest_zip_archive,
    // well_logs -> ingest_well_logs. None of the three has the field, and a
    // control whose value is silently discarded is worse than no control.
    for (const cat of ['reports', 'archive', 'well_logs'] as Category[]) {
      expect(supportsCrsOverride(cat), cat).toBe(false);
    }
    expect(supportsCrsOverride(null)).toBe(false);
  });
});
