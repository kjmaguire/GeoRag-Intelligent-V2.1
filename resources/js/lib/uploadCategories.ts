/**
 * Upload categories, mirroring App\Http\Controllers\Api\V1\UploadController.
 *
 * This exists because the same knowledge was duplicated in two places and
 * drifted in both directions:
 *
 *   - NewProject.tsx offered `spatial` for .kmz/.kml, which the backend has
 *     never accepted, and offered `seismic`/`xyz`, which it refuses — so the
 *     picker promised uploads that came back 422.
 *   - DataImportWizard.tsx accepted only PDF/TIFF/ZIP and refused everything
 *     else client-side, so the drill and GIS formats were unreachable from
 *     that screen even after the API started accepting them.
 *
 * One module, imported by both. When UploadController::CATEGORIES changes,
 * this is the single file that changes with it.
 */

export type Category =
  | 'reports'
  | 'archive'
  | 'collars'
  | 'surveys'
  | 'lithology'
  | 'samples'
  | 'excel'
  | 'spatial'
  | 'tables'
  | 'well_logs'
  | 'seismic'
  | 'xyz';

/** Extensions each category accepts. Mirrors UploadController::CATEGORIES. */
export const CATEGORY_EXTS: Record<Category, string[]> = {
  // `.rrd` is an ERDAS pyramid — normally a derived companion of a raster,
  // but the only surviving copy of the image when its parent is missing,
  // which is how it arrived in a real delivery. tiff_normalize extracts the
  // finest level.
  // `.jpg`/`.jpeg` are scanned sheets: RedStar's is the legend for a 1990
  // geological map, i.e. nothing but the unit descriptions that make the map
  // readable. They wrap to PDF through the same Pillow path as a TIFF and,
  // carrying no CRS, always reach OCR rather than being filed as a data grid.
  reports: ['pdf', 'tif', 'tiff', 'rrd', 'jpg', 'jpeg'],
  archive: ['zip'],
  collars: ['csv', 'txt', 'tsv'],
  surveys: ['csv', 'txt', 'tsv'],
  lithology: ['csv', 'txt', 'tsv'],
  samples: ['csv', 'txt', 'tsv'],
  excel: ['xlsx', 'xls', 'xlsm'],
  // A dBASE table with no `.shp` beside it is not a shapefile sidecar, it is
  // an attribute table, and it routes to ingest_tabular. Its own category
  // rather than a slot in `excel` or in the four drill categories: nothing
  // in the extension says which drill table a .dbf holds, so listing it
  // under collars/surveys/lithology/samples would make categoryForExtension
  // pick one arbitrarily and pin a wrong sheet_type on every auto-routed
  // file. A `.dbf` beside a same-stem `.shp` never reaches here —
  // groupShapefiles() zips it into the shapefile bundle first, and that
  // sibling is the only thing that discriminates the two cases.
  // `.dat` is here for the same reason: a MapInfo attribute half IS a dBASE
  // file and reads standalone once its master is absent. The long-standing
  // comment that ".dat is already claimed by the retired xyz category"
  // describes a constraint that does not exist — UploadController consults
  // RETIRED_CATEGORIES by category NAME only, never by extension. The proof
  // already ships: `txt` sits in retired `xyz` AND in live `collars`, and
  // .txt uploads work today.
  // `.mdb`/`.accdb` are here too: an Access database is a container of
  // TABLES, and it fans out to one attribute table per Access table.
  tables: ['dbf', 'dat', 'mdb', 'accdb'],
  // ZIP is here because a shapefile is never one file — .shp/.shx/.dbf/.prj
  // travel together and a lone .shp cannot be read without them.
  // MapInfo: `.tab` and `.mif` are the ENTRY POINTS GDAL opens. Their
  // geometry/index companions (.map/.id/.ind) are absent because they cannot
  // be read alone, and `.mid` because it opens on its own — accepting it
  // would ingest a MIF/MID pair twice. `.dat` is NOT in that group: it is the
  // attribute half, a whole dBASE table, and it lives in `tables` above.
  // shapefileBundle.ts zips the complete set under this category.
  spatial: [
    'geojson', 'json', 'shp', 'gpkg', 'gml', 'gpx', 'dxf', 'dgn',
    'fgb', 'gdb', 'zip', 'qgs', 'qgz', 'tab', 'mif',
    // Surpac string file — mine-design strings (vein outlines, level plans).
    // No CRS of its own, so it needs an EPSG the same way a .dxf does.
    'str',
  ],
  well_logs: ['las'],
  seismic: ['sgy', 'segy'],
  xyz: ['xyz', 'dat', 'txt'],
};

export const CATEGORY_LABEL: Record<Category, string> = {
  reports: 'NI 43-101 / reports & scans (PDF, TIFF, JPEG, ERDAS RRD)',
  archive: 'Archive of mixed files (ZIP)',
  collars: 'Drill collars (CSV)',
  surveys: 'Down-hole surveys (CSV)',
  lithology: 'Lithology logs (CSV)',
  samples: 'Assay samples (CSV)',
  excel: 'Excel workbooks (XLSX)',
  tables: 'Attribute table (DBF, MapInfo DAT, Access MDB)',
  spatial: 'Spatial / GIS (SHP, MapInfo, GeoPackage, GeoJSON, QGIS, Surpac, ZIP)',
  well_logs: 'Well logs (LAS)',
  seismic: 'Seismic (SEG-Y)',
  xyz: 'XYZ grids / point data',
};

/**
 * Categories the backend still refuses with 422
 * (UploadController::RETIRED_CATEGORIES).
 *
 * Offering one means the user picks a category, waits through the upload and
 * is then told no. Filter these out of every picker, and move an entry out of
 * here the moment its workflow ships.
 */
export const RETIRED_CATEGORIES = new Set<Category>(['seismic', 'xyz']);

/** Categories a user may actually choose. */
export const LIVE_CATEGORIES = (Object.keys(CATEGORY_LABEL) as Category[]).filter(
  (c) => !RETIRED_CATEGORIES.has(c),
);

/** Image formats the backend rejects outright. TIFF is NOT one of them — it
 *  routes through `reports` and is normalised to PDF (ADR-0005). */
export const UNSUPPORTED_EXTS = new Set(['jpg', 'jpeg', 'png', 'gif', 'bmp']);

export function extensionOf(filename: string): string {
  return filename.split('.').pop()?.toLowerCase() ?? '';
}

/**
 * Best category for a file extension, or null when nothing accepts it.
 *
 * Order matters where an extension is ambiguous. `.zip` resolves to
 * `archive` (a mixed bundle of reports) rather than `spatial`, because that
 * is the commoner case and the user can override in the picker. `.csv`
 * resolves to `collars` for the same reason — it is the file people upload
 * first, and the other drill types are one click away.
 */
export function categoryForExtension(ext: string): Category | null {
  if (UNSUPPORTED_EXTS.has(ext)) return null;

  const preference: Category[] = [
    'reports',
    'archive',
    'collars',
    'surveys',
    'lithology',
    'samples',
    'excel',
    'tables',
    'spatial',
    'well_logs',
  ];

  for (const cat of preference) {
    if (RETIRED_CATEGORIES.has(cat)) continue;
    if (CATEGORY_EXTS[cat].includes(ext)) return cat;
  }
  return null;
}

/** Every extension any live category accepts — for an `accept=` attribute. */
export function acceptedExtensions(): string[] {
  const all = new Set<string>();
  for (const cat of LIVE_CATEGORIES) {
    for (const ext of CATEGORY_EXTS[cat]) all.add(ext);
  }
  return [...all].sort();
}

/* ------------------------------------------------------------------ *
 * Per-file CRS override (`source_epsg`)
 * ------------------------------------------------------------------ */

/**
 * The EPSG range the API accepts.
 *
 * Not a number this module invented. It is the rule already written in
 * app/Http/Requests/StoreQueryRequest.php (`min:1024`, `max:32767`), which in
 * turn matches the database CHECK on `silver.spatial_features.crs_epsg_native`
 * and `silver.geophysics_surveys.crs_epsg`. Validating to a different bound
 * here would put a fourth definition of "a valid CRS" in the codebase, and the
 * one that disagrees is always the one the user meets first.
 *
 * The wire representation is an INTEGER. Never a string like 'EPSG:26904' —
 * the tabular ingest path has taken `source_epsg` as an integer since it was
 * built, and two spellings of one concept across two ingest paths that share
 * this UI is how the categories drifted in the first place.
 */
export const EPSG_MIN = 1024;
export const EPSG_MAX = 32767;

export interface EpsgParse {
  /** Set only when the text is a legal EPSG code. */
  epsg?: number;
  /** Set only when the text is present and illegal. Render it; do not upload. */
  error?: string;
}

/**
 * Parse a user-typed EPSG code.
 *
 * Empty is not an error — the override is optional, and the file's own
 * declared CRS wins over it in every case where the file has one.
 */
export function parseEpsg(text: string): EpsgParse {
  const trimmed = text.trim();
  if (trimmed === '') return {};
  if (!/^\d+$/.test(trimmed)) {
    return { error: `“${trimmed}” is not an EPSG code — enter the number only, e.g. 26904.` };
  }
  const n = Number(trimmed);
  if (n < EPSG_MIN || n > EPSG_MAX) {
    return { error: `EPSG codes must be in the range ${EPSG_MIN}-${EPSG_MAX}.` };
  }
  return { epsg: n };
}

/**
 * Whether a per-file CRS override is meaningful for this category.
 *
 * Exactly the categories UploadController forwards `source_epsg` on: the ones
 * mapped to ingest_spatial or ingest_tabular, the two workflows whose input
 * model declares the field. Offering the control anywhere else would render
 * an input whose value is dropped in transit, which is worse than no input.
 *
 * `spatial` is why the override exists — a vector file that declares no
 * coordinate system is now refused outright rather than silently filed at
 * SRID 4326, which is what put an Alaskan shapefile at longitude 400,797.
 * A shapefile or MapInfo bundle zipped by shapefileBundle.ts uploads under
 * `spatial` too, not `archive`, so a ZIP from this screen reaches the same
 * refusal and the same escape hatch.
 *
 * The tabular categories matter for a quieter reason: nothing has ever sent
 * ingest_tabular a source_epsg, so every drill CSV the platform has ingested
 * silently assumed EPSG:32613.
 *
 * `reports` (PDF/TIFF), `archive` (ingest_zip_archive) and `well_logs` have
 * no such field and are deliberately absent.
 */
export function supportsCrsOverride(category: Category | null): boolean {
  return (
    category === 'spatial' ||
    category === 'collars' ||
    category === 'surveys' ||
    category === 'lithology' ||
    category === 'samples' ||
    category === 'excel' ||
    category === 'tables'
  );
}
