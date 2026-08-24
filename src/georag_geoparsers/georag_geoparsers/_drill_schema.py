"""The drill-table field vocabulary, in one place.

## Why this module exists

These alias lists existed in THREE copies: the polars parsers in this
package, the stdlib re-implementation in
``app/services/ingest/csv_collar_ingester.py``, and the retired Dagster
tree. Only the first two run, and they had already drifted — the writer's
docstring still advertised "matching is case-sensitive" after the
classifier had stopped being so.

Nothing imports polars to read this file, which is the other reason it is
separate: ``_sheet_classifier`` used to import all four parsers purely to
reach their alias dicts, dragging polars, geopandas and rasterio into any
process that wanted to guess a sheet type.

## On widening the lists

The aliases below are deliberately generous. The failure mode they exist
to prevent is silent and total: an unrecognised header means a required
field is unmapped, which means the file is rejected in full with a message
telling a geologist to go and rename their columns. The opposite failure —
mapping a column we should have ignored — is visible in the ingest report
and correctable per file, and the range checks catch values that landed in
the wrong field.

``_header_match.normalize_header`` folds case, separators and unit
suffixes, so only distinct SKELETONS need listing here. ``Hole_ID`` covers
``Hole ID``, ``HOLE-ID``, ``holeId`` and ``hole.id``; ``Depth_m`` is
already covered by ``Depth``.
"""

from __future__ import annotations

#: Every drill table is keyed by the hole it was logged in, and every
#: vendor spells it differently. Shared by all four schemas so a spelling
#: learned once is understood everywhere.
HOLE_ID_ALIASES: list[str] = [
    "HoleID", "Hole_ID", "HOLEID", "hole_id", "Hole", "Hole_No", "Hole_Number",
    "Hole_Name", "DrillHole", "Drill_Hole", "DH_ID", "DHID", "BH_ID", "BHID",
    "DDH", "DDH_ID", "Borehole", "Borehole_ID", "Collar_ID", "CollarID",
    "Location_ID", "Site_ID",
]

#: Interval tops and bottoms, shared by lithology and sample.
FROM_DEPTH_ALIASES: list[str] = [
    "From", "FromDepth", "From_Depth", "Depth_From", "From_m",
    "Start_Depth", "Depth_Top", "Top",
]
TO_DEPTH_ALIASES: list[str] = [
    "To", "ToDepth", "To_Depth", "Depth_To", "To_m",
    "End_Depth", "Depth_Base", "Base", "Bottom",
]

COLLAR_ALIASES: dict[str, list[str]] = {
    "hole_id": HOLE_ID_ALIASES,
    # Longitude/Latitude are listed as easting/northing because that is
    # what they are — the x and y of a geographic CRS. The range checks
    # below switch bounds on the values themselves rather than assuming
    # a projected grid, so a decimal-degree collar table is no longer
    # rejected row by row for being "out of UTM range".
    "easting": [
        "Easting", "EAST", "East_X", "X", "X_Coord", "X_UTM", "UTM_E", "mE",
        "Local_X", "Grid_X", "Longitude", "Long", "Lon",
    ],
    "northing": [
        "Northing", "NORTH", "North_Y", "Y", "Y_Coord", "Y_UTM", "UTM_N", "mN",
        "Local_Y", "Grid_Y", "Latitude", "Lat",
    ],
    "elevation": [
        "Elevation", "ELEV", "RL", "Z", "Z_Coord", "Collar_RL",
        "Collar_Elevation", "Collar_Height", "Altitude", "Height",
        "MASL", "Topo",
    ],
    "total_depth": [
        "TotalDepth", "Total_Depth", "DEPTH", "TD", "MaxDepth", "EOH",
        "EOH_Depth", "Final_Depth", "Hole_Length", "Length",
    ],
    "azimuth": ["Azimuth", "AZI", "AZ", "Bearing", "Collar_Azimuth", "Grid_Azimuth"],
    "dip": ["Dip", "DIP", "Inclination", "INC", "Collar_Dip", "Plunge"],
    "hole_type": ["HoleType", "Hole_Type", "Type", "DrillType", "Drill_Method"],
    "drill_date": [
        "Date", "DrillDate", "Drill_Date", "StartDate", "Start_Date",
        "Date_Drilled", "Completion_Date",
    ],
    "status": ["Status"],
}

SURVEY_ALIASES: dict[str, list[str]] = {
    "hole_id": HOLE_ID_ALIASES,
    "depth": [
        "Depth", "DEPTH", "Survey_Depth", "MD", "Measured_Depth",
        "At_Depth", "Station",
    ],
    "azimuth": ["Azimuth", "AZI", "AZ", "Bearing"],
    "dip": ["Dip", "DIP", "Inclination", "INC", "Plunge"],
    "survey_method": ["Method", "SurveyMethod", "Survey_Method", "Instrument", "Tool"],
}

LITHOLOGY_ALIASES: dict[str, list[str]] = {
    "hole_id": HOLE_ID_ALIASES,
    "from_depth": FROM_DEPTH_ALIASES,
    "to_depth": TO_DEPTH_ALIASES,
    "lithology_code": [
        "Lithology", "LithCode", "Lith_Code", "Lith", "Litho", "RockCode",
        "Rock_Code", "RockType", "Rock_Type", "Unit", "Formation",
    ],
    "lithology_description": [
        "Description", "LithDesc", "Lithology_Description", "Lith_Description",
        "Desc", "Log_Description", "Comments", "Remarks", "Notes",
    ],
    "grain_size": ["GrainSize", "Grain_Size", "Grain", "Texture"],
    "color": ["Color", "Colour"],
    "hardness": ["Hardness", "Strength"],
    "rqd": ["RQD", "RockQualityDesignation", "RQD_Pct"],
    "recovery": ["Recovery", "CoreRecovery", "Core_Recovery", "Rec"],
    "weathering": ["Weathering", "Weathered", "Alteration_Weathering"],
}

SAMPLE_ALIASES: dict[str, list[str]] = {
    "hole_id": HOLE_ID_ALIASES,
    "from_depth": FROM_DEPTH_ALIASES,
    "to_depth": TO_DEPTH_ALIASES,
    "sample_type": ["SampleType", "Sample_Type", "Type", "Samp_Type", "Sample_Class"],
    "lab_id": ["LabID", "Lab_ID", "Lab", "LabNumber", "Lab_No", "Certificate", "Cert_No"],
    "qaqc_type": ["QAQC", "QC", "QC_Type", "Control_Type"],
    "sample_id": [
        "SampleID", "Sample_ID", "Sample_Number", "SampleNum", "Sample_No",
        "SampleNo", "Samp_ID", "Sample",
    ],
}

#: A collar needs an identity and a position. Elevation was required until
#: 2026-08-24 and should not have been: ``silver.collars.elevation`` is
#: nullable, the writer already reads it with ``.get()``, and plenty of
#: real collar tables carry no elevation column at all because the value is
#: draped from a DEM later. Requiring it rejected every row of such a file
#: and told the user to add a column their survey does not produce.
COLLAR_REQUIRED: frozenset[str] = frozenset({"hole_id", "easting", "northing"})
SURVEY_REQUIRED: frozenset[str] = frozenset({"hole_id", "depth", "azimuth", "dip"})
LITHOLOGY_REQUIRED: frozenset[str] = frozenset(
    {"hole_id", "from_depth", "to_depth", "lithology_code"}
)
SAMPLE_REQUIRED: frozenset[str] = frozenset(
    {"hole_id", "from_depth", "to_depth", "sample_type"}
)


def schemas() -> dict[str, tuple[dict[str, list[str]], frozenset[str]]]:
    """``{sheet_type: (aliases, required)}`` for the four drill layouts."""
    return {
        "collar": (COLLAR_ALIASES, COLLAR_REQUIRED),
        "survey": (SURVEY_ALIASES, SURVEY_REQUIRED),
        "lithology": (LITHOLOGY_ALIASES, LITHOLOGY_REQUIRED),
        "sample": (SAMPLE_ALIASES, SAMPLE_REQUIRED),
    }


# ---------------------------------------------------------------------------
# Coordinate sanity bounds
# ---------------------------------------------------------------------------

#: Bounds for coordinates that are angles.
GEOGRAPHIC_BOUNDS: dict[str, tuple[float, float]] = {
    "easting": (-180.0, 180.0),
    "northing": (-90.0, 90.0),
}

#: Bounds for coordinates that are lengths on a projected grid.
#:
#: Deliberately enormous. The previous bounds — easting 100,000..900,000,
#: northing 0..10,000,000 — encoded ONE projection family (northern-
#: hemisphere UTM in metres) as though it were a definition of validity,
#: and quietly rejected everything else: a local mine grid numbered from
#: 5,000, State Plane in survey feet (eastings well past 900,000), and any
#: southern-hemisphere or negative-easting system.
#:
#: This is an absurdity check, not a CRS check. It catches a text column
#: cast to a huge float, or a depth pasted into a coordinate. Whether the
#: coordinates are in the RIGHT system is measured downstream against the
#: project CRS, which is where that question belongs.
PROJECTED_BOUNDS: dict[str, tuple[float, float]] = {
    "easting": (-20_100_000.0, 20_100_000.0),
    "northing": (-20_100_000.0, 20_100_000.0),
}

#: Deepest mine to highest summit, in either metres or feet. Feet is why
#: the ceiling is not 8,900: Everest is 29,032 ft.
ELEVATION_BOUNDS: tuple[float, float] = (-12_000.0, 30_000.0)


#: Header skeletons that NAME a coordinate as an angle, and as a length.
#:
#: Used to catch a pairing no range check can: ``Easting`` beside
#: ``LATITUDE``. Each column is individually plausible, and the row
#: (495000.0, 57.123) sits inside projected bounds, so nothing downstream
#: would object — but a UTM easting paired with a decimal-degree northing
#: is not a position, and writing it to the map puts the hole 57 metres
#: north of the equator.
GEOGRAPHIC_COLUMN_SKELETONS: frozenset[str] = frozenset({
    "longitude", "long", "lon", "latitude", "lat",
})
PROJECTED_COLUMN_SKELETONS: frozenset[str] = frozenset({
    "easting", "east", "eastx", "utme", "me", "gridx", "localx", "xutm",
    "northing", "north", "northy", "utmn", "mn", "gridy", "localy", "yutm",
})


def column_coordinate_family(column_name: str) -> str:
    """``"geographic"`` / ``"projected"`` / ``"unknown"`` for a header.

    ``"unknown"`` is the honest answer for ``X``, ``Y`` and ``X_Coord``,
    which name an axis without saying what is measured along it. Those
    fall through to the value-based check.
    """
    from georag_geoparsers._header_match import normalize_header

    skeleton = normalize_header(column_name)
    if skeleton in GEOGRAPHIC_COLUMN_SKELETONS:
        return "geographic"
    if skeleton in PROJECTED_COLUMN_SKELETONS:
        return "projected"
    return "unknown"


def coordinate_family_conflict(
    easting_column: str | None,
    northing_column: str | None,
) -> tuple[str, str] | None:
    """The two families when the mapped columns disagree, else ``None``.

    A disagreement is returned rather than resolved: there is no way to
    tell from here whether the file means degrees or metres, and guessing
    would place the hole somewhere definite and wrong.
    """
    if not easting_column or not northing_column:
        return None

    east_family = column_coordinate_family(easting_column)
    north_family = column_coordinate_family(northing_column)

    if "unknown" in (east_family, north_family) or east_family == north_family:
        return None
    return (east_family, north_family)


def detect_coordinate_mode(
    eastings: list[float | None],
    northings: list[float | None],
) -> str:
    """``"geographic"`` when the coordinates are angles, else ``"projected"``.

    Decided from the values rather than from a declared CRS because the
    parser is frequently handed neither — a bare CSV declares nothing, and
    the project CRS describes where the holes are, not what units the file
    writes them in.

    A file is only read as geographic when EVERY populated pair fits inside
    the lon/lat envelope. One row at easting 512,000 is enough to make the
    file projected, which is the safe direction: projected bounds are wide
    enough to accept degrees, so a misread costs nothing, while reading a
    UTM file as geographic would reject all of it.
    """
    pairs = [
        (e, n)
        for e, n in zip(eastings, northings, strict=False)
        if e is not None and n is not None
    ]
    if not pairs:
        return "projected"

    if all(-180.0 <= e <= 180.0 and -90.0 <= n <= 90.0 for e, n in pairs):
        return "geographic"
    return "projected"


def coordinate_bounds(mode: str) -> dict[str, tuple[float, float]]:
    """Easting/northing bounds for a mode from :func:`detect_coordinate_mode`."""
    return dict(GEOGRAPHIC_BOUNDS if mode == "geographic" else PROJECTED_BOUNDS)
