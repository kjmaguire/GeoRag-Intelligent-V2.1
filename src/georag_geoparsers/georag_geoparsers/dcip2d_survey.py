"""Assemble a UBC-GIF DCIP2D export into one record a caller can persist.

``dcip2d_parser`` reads the individual files. This module answers the next
question — where does the result GO — and for the delivery in hand the honest
answer is "into survey metadata, with the georeference recorded as UNRESOLVED",
because the numbers that would put the section on a map are not on disk. The
argument for the destination and the evidence for that verdict are both below,
measured from the Centennial L3750N export and the grid's station file.

## Destination: silver.geophysics_surveys

Two candidates were checked against what actually runs, not against what has a
migration. This project has twice been bitten by a table that looked reachable
because it existed.

**silver.spatial_features — rejected, and not because of a dead pipeline.**
It is the LIVE one: ``ingest_spatial`` is a registered Hatchet workflow and the
``spatial`` upload category is accepted today. It is still the wrong home,
because its whole purpose is ``geom geometry(Geometry, 4326)`` — a GIST index
and an MVT function are its only consumers. This delivery has no 4326 geometry
to put there and cannot acquire one (three reasons below). ``geom`` happens to
be NULL-able, which makes the failure worse rather than better: a NULL-geometry
row is invisible to every consumer of that table while making the survey look
ingested. That is the exact shape of loss this pipeline is not allowed to have.

**silver.geophysics_surveys — chosen.** It is the only table whose columns fit
an ungeoreferenced survey without lying: ``survey_type`` CHECKs in ``'IP'``,
``line_ids`` is ``text[]``, and ``aoi_geom`` and ``crs_epsg`` are BOTH nullable,
so "we do not know where this is" is representable rather than fabricated.

A writer for it exists and is real code:
``src/dagster/georag_dagster/assets/silver_geophysics.py`` takes a JSON payload
and upserts one row on ``(workspace_id, survey_name)``. What does NOT exist is
a live CALLER — the ``geophysics`` upload category has been in
``UploadController::RETIRED_CATEGORIES`` since the Dagster services stopped on
2026-07-28 and answers 422. So the trap here is one level deeper than "table
with no writer": the writer is fine, its trigger was retired.

That is why this destination wins anyway. The blocker at
``geophysics_surveys`` is a missing trigger — a wiring change, and a small one,
because :meth:`Dcip2dSurvey.to_geophysics_survey_payload` emits exactly the dict
that writer already consumes, key for key, so nothing new has to be written to
land these rows. The blocker at ``spatial_features`` is missing coordinates,
which no amount of wiring fixes and which nothing on disk can supply.

## The georeference verdict: three independent blockers

The join was expected to work like this: the models and the observed data live
on a 1-D chainage along one line, ``export_UTM.xls`` carries stations with real
UTM X/Y/Z, so station chainage ties the two together. It does not, and it fails
three separate ways. Every number here was measured before the code was written.

1. **The station file does not contain this line.** All 24 rows carry
   ``LineNumber`` 4250 and ``StationName`` "4250N ...". The export is line
   3750 N. Zero of 24 rows are on it, so the join returns nothing at all — not
   a sparse match, an empty one.

2. **The projection is unusable even where the join lands.** Every row records
   ``Projections_Name`` "Trivial UTM": a projection family with no zone and no
   datum. A UTM northing of 6,130,169 m is about 55.3°N in ANY northern zone,
   and the zone alone decides whether an easting of 404,513 m is in Alaska,
   Manitoba, Britain or Siberia. Without it there is no EPSG code, so nothing
   can be transformed to 4326 — which is what blocks ``spatial_features`` for
   line 4250 N too, the line the file DOES cover. (``IP.inp`` records a run
   directory ``C:\\Jobs\\AES_AK_2005\\...``. "AK" is a hint about a job folder,
   not a coordinate reference system, and this module does not treat it as one.)

3. **The chainage axis is not the station axis.** The grid's stations are
   pickets on an exact 50 m interval, 4600 to 5750. The 22 distinct electrode
   chainages are 4500.00 to 5494.88 with spacings running 34.89 m to 57.58 m,
   and NONE of them equals a station number. Three of them (4500.00, 4547.55,
   4596.62) fall below the lowest picket in the file entirely. So the join is
   not a lookup, it is an interpolation — and interpolation needs the per-line
   relation between picket number and ground metres, which is measured, not
   assumed: on line 4250 N a nominal 50 m picket step measures anywhere from
   37.97 m to 64.19 m between consecutive UTM positions. That relation is a
   property of one line's terrain and cannot be carried to another.

Blockers 1 and 2 stop the OBSERVED data being placed. The models have a fourth
problem on top, handled separately by :class:`MeshGeoreference`: the 55x20 cells
are positions in a mesh, and ``IP.inp`` names the two files that define it —
``dcinv2d.msh`` and ``L3750dz.txt`` — neither of which was delivered. The
export therefore cannot place a model cell even on the CHAINAGE axis, let alone
on the ground. The arithmetic is suggestive (994.88 m of data across 55 cells
leaves room for a ~25 m core plus the padding a UBC mesh always carries) and it
is deliberately not acted on: a mesh origin guessed to the nearest plausible
value produces a section that hangs in the right-looking place and is wrong,
which is indistinguishable from correct until someone drills it.

## What would actually close this

Nothing here is unrecoverable in principle — it is missing files, not lost
information — so the verdict is recorded as reasons a human can act on rather
than as a silent NULL:

  * the station rows for line 3750 N, in the same shape as the 4250 N ones;
  * the grid's projection as a zone and datum (or an EPSG asserted at upload,
    which the upload path already supports for files that declare none);
  * ``dcinv2d.msh`` and ``L3750dz.txt`` from the inversion working directory.

``CEN_L3750_IP.mdb`` sits beside the export and is a Jet database of this
line's IP survey. It is the most likely home of the first two and this module
does not read it — noted here so the next person does not re-derive the lead.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import numpy as np

from georag_geoparsers.dcip2d_parser import (
    INP_UNSET,
    Dcip2dModel,
    read_dcip2d_data,
    read_dcip2d_model,
    read_inp,
)

logger = logging.getLogger(__name__)

PARSER_NAME = "dcip2d_survey"
PARSER_VERSION = "1.0.0"

#: The one value of ``silver.geophysics_surveys.survey_type`` a DC/IP inversion
#: can carry. The column is CHECK-constrained to seven strings and this is a
#: schema contract, not a label — anything else fails the INSERT.
SURVEY_TYPE = "IP"

#: Model files this module will read. The stage must be a three-digit iteration
#: or ``chg``, which is what excludes the two files ``IP.inp`` names as INPUTS
#: to the same run: ``dcinv2d.msh`` and ``dcinv2d.con``. That exclusion matters
#: asymmetrically. A ``.msh`` would raise (its body is not ``nx * nz`` values),
#: but a ``.con`` is a starting conductivity model in the identical format and
#: would parse cleanly into something indistinguishable from a RESULT — an
#: inversion's input silently reported as its output.
_MODEL_FILE = re.compile(r"^(dcinv2d|ipinv2d)\.(\d{3}|chg)$", re.IGNORECASE)

#: The final chargeability model, which is NOT the last numbered iteration:
#: measured on this export, ``ipinv2d.chg`` differs from ``ipinv2d.016``
#: (earth median 6.28597 vs 6.22404 mV/V, max 81.8788 vs 72.7252).
FINAL_CHARGEABILITY_STAGE = "chg"

#: Observed-data files. A DCIP2D export splits them by survey geometry across
#: ``.rdt`` and its ``.rdtm*`` variants, so the test is on the prefix.
_OBSERVED_SUFFIX = ".rdt"

#: ``(needle, quantity)``, first match wins, tested against the lower-cased
#: title line. Ordered rather than a dict because a title is free text a
#: contractor typed and more than one needle can appear in it.
_QUANTITY_RULES: tuple[tuple[str, str], ...] = (
    ("normalized potential", "normalized_potential"),
    ("chargeability", "chargeability"),
)

#: What a title says when neither rule fires. Kept as a value rather than None
#: so it survives into ``processing_notes`` and a reader sees that the file was
#: read and its quantity was not recognised, instead of seeing nothing.
QUANTITY_UNKNOWN = "unknown"

#: ``Line 3750 N`` inside an observed-data title. The title is the contractor's
#: own statement of which line the file holds, which is why it outranks the
#: directory name below.
_LINE_IN_TITLE = re.compile(r"\bline\s+(\d+(?:\.\d+)?)\s*([NSEW])\b", re.IGNORECASE)

#: A directory named for a line: ``L3750N``, or ``3750N``.
_LINE_IN_DIRNAME = re.compile(r"^L?(\d+(?:\.\d+)?)\s*([NSEW])$", re.IGNORECASE)

#: Columns ``read_dcip2d_stations`` cannot work without. ``Z`` is NOT among
#: them: an IP station with easting and northing and no elevation is still a
#: station, and dropping the row would lose a position over a missing height.
REQUIRED_STATION_COLUMNS = ("LineNumber", "StationNumber", "X", "Y")

#: ``Projections_Name`` values that resolve to an EPSG code.
#:
#: Empty, and that emptiness is the finding rather than a TODO. The delivery in
#: hand records "Trivial UTM" — see blocker 2 in the module docstring. A name
#: that DOES carry both a zone and a datum belongs to ``spatial_parser``, which
#: already owns CRS resolution and confidence scoring for this package; this
#: table is the hook, and it stays empty until a delivery earns an entry.
#: Guessing an entry is how a section ends up 200 km from the drilling.
PROJECTION_EPSG: dict[str, int] = {}


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Dcip2dStation:
    """One surveyed picket from the grid's station file.

    ``station_number`` is a picket label on the grid's local numbering, NOT a
    distance — see blocker 3 in the module docstring. ``easting`` and
    ``northing`` are the file's numbers verbatim in whatever projection
    ``projection_name`` describes; they are deliberately not called ``x``/``y``
    in 4326 terms, because this module never establishes that they are.
    """

    grid_name: str
    line_number: float
    series: str            # 'N' — the line's direction letter, e.g. 3750 N
    station_number: float
    station_name: str      # '4250N 4600E' as written
    line_type: str         # 'Cross Line'
    projection_name: str   # 'Trivial UTM' — names no zone and no datum
    easting: float
    northing: float
    elevation: float | None


@dataclass(frozen=True)
class ObservedSplit:
    """One observed-data file: its header, its quantity, and its readings.

    ``records`` may legitimately be empty. Three of the four splits in this
    export are header-only stubs for geometries the survey never produced, and
    ``dcip2d_parser`` treats that as information rather than a parse error.
    """

    filename: str
    title: str
    array_type: str
    quantity: str          # one of _QUANTITY_RULES' values, or QUANTITY_UNKNOWN
    records: tuple[tuple[float, float, float, float, float], ...]
    chainages: tuple[float, ...]   # distinct electrode positions, ascending

    @property
    def record_count(self) -> int:
        return len(self.records)

    @property
    def is_stub(self) -> bool:
        return not self.records


@dataclass(frozen=True)
class ModelFile:
    """One inversion result, with the stage it came from.

    ``iteration`` is None for ``ipinv2d.chg``, which is a named final product
    rather than a numbered step — see FINAL_CHARGEABILITY_STAGE.
    """

    filename: str
    family: str            # 'dcinv2d' (conductivity) | 'ipinv2d' (chargeability)
    stage: str             # '011' | '030' | 'chg'
    iteration: int | None
    model: Dcip2dModel


@dataclass(frozen=True)
class MeshGeoreference:
    """Can the 55x20 model cells be placed on an axis? Here: no.

    Separate from :class:`ChainageJoin` because it fails for a different reason
    and would survive that join being fixed. Delivering the station rows for
    line 3750 N places the ELECTRODES; it does nothing for the models, which
    need the mesh and topography files ``IP.inp`` names.
    """

    mesh_file: str | None            # as named in the .inp, verbatim
    mesh_delivered: bool
    topography_file: str | None
    topography_delivered: bool
    unresolved_reasons: tuple[str, ...]

    @property
    def resolved(self) -> bool:
        return not self.unresolved_reasons


@dataclass(frozen=True)
class ChainageJoin:
    """The station join and, when it fails, every reason it failed.

    All reasons are collected rather than short-circuited on the first. A
    caller told only "no stations for this line" would deliver the missing
    rows, re-run, and discover the projection problem on the second pass and
    the picket-axis problem on the third.
    """

    station_file: str | None
    stations_read: int   # ACCEPTED rows; rejects are logged per row by the reader
    lines_in_station_file: tuple[float, ...]
    stations_on_line: tuple[Dcip2dStation, ...]
    grid_station_numbers: tuple[float, ...]   # every picket in the file, any line
    chainages: tuple[float, ...]              # every electrode position observed
    exact_matches: tuple[float, ...]          # chainages that ARE picket numbers
    projection_names: tuple[str, ...]
    crs_epsg: int | None
    unresolved_reasons: tuple[str, ...]

    @property
    def resolved(self) -> bool:
        return not self.unresolved_reasons


@dataclass(frozen=True)
class Dcip2dSurvey:
    """One DCIP2D export, assembled and ready to persist.

    Everything the export contains, plus an explicit verdict on whether any of
    it can be placed on the ground. ``to_geophysics_survey_payload`` turns it
    into the JSON the ``silver_geophysics`` writer already consumes.
    """

    export_dir: Path
    line_id: str                 # canonical 'L3750N'
    line_number: float
    series: str
    array_type: str
    observed: tuple[ObservedSplit, ...]
    models: tuple[ModelFile, ...]
    manifest: dict[str, str]
    join: ChainageJoin
    mesh: MeshGeoreference

    # -- convenience views ------------------------------------------------

    @property
    def observation_count(self) -> int:
        """Readings across every split — the stubs contribute zero."""
        return sum(split.record_count for split in self.observed)

    @property
    def stub_count(self) -> int:
        return sum(1 for split in self.observed if split.is_stub)

    @property
    def final_conductivity(self) -> ModelFile | None:
        """The highest-numbered ``dcinv2d`` iteration, i.e. the DC result."""
        numbered = [m for m in self.models if m.family == "dcinv2d" and m.iteration is not None]
        if not numbered:
            return None
        return max(numbered, key=lambda m: m.iteration)

    @property
    def final_chargeability(self) -> ModelFile | None:
        """``ipinv2d.chg`` when present, else the highest-numbered iteration.

        The preference is not cosmetic: ``.chg`` is a distinct model from the
        last numbered step, so taking ``max(iteration)`` when ``.chg`` exists
        would report a different chargeability section than the run's own final
        product.
        """
        for model in self.models:
            if model.family == "ipinv2d" and model.stage.lower() == FINAL_CHARGEABILITY_STAGE:
                return model
        numbered = [m for m in self.models if m.family == "ipinv2d" and m.iteration is not None]
        if not numbered:
            return None
        return max(numbered, key=lambda m: m.iteration)

    @property
    def is_georeferenced(self) -> bool:
        """True only if BOTH the electrodes and the model cells can be placed."""
        return self.join.resolved and self.mesh.resolved

    # -- the fields that carry the numbers into the database --------------

    @property
    def summary_lines(self) -> tuple[str, ...]:
        """``processing_notes`` as separate lines, so tests can assert one.

        This is the field that carries the measured shape of the delivery into
        a place a geologist can read. The georeference verdict is on it in
        words, because ``aoi_geom`` and ``crs_epsg`` being NULL is
        indistinguishable from nobody having tried.
        """
        lines = [
            f"UBC-GIF DCIP2D inversion, line {self.line_id}, {self.array_type} array.",
        ]

        carriers = [s for s in self.observed if not s.is_stub]
        quantities = ", ".join(sorted({s.quantity for s in carriers})) or "none"
        lines.append(
            f"Observed: {self.observation_count} readings in {len(carriers)} of "
            f"{len(self.observed)} geometry splits ({quantities}); "
            f"{self.stub_count} split(s) exported with zero readings."
        )

        if self.join.chainages:
            first, last = self.join.chainages[0], self.join.chainages[-1]
            lines.append(
                f"Electrodes: {len(self.join.chainages)} positions on a 1-D chainage "
                f"{first:.2f}-{last:.2f} m ({last - first:.2f} m of line)."
            )

        if self.models:
            shapes = {(m.model.nx, m.model.nz) for m in self.models}
            air = {int(m.model.air_mask.sum()) for m in self.models}
            shape_text = ", ".join(f"{nx}x{nz}" for nx, nz in sorted(shapes))
            air_text = ", ".join(str(a) for a in sorted(air))
            lines.append(
                f"Models: {len(self.models)} files on mesh {shape_text} "
                f"({'one shared mesh' if len(shapes) == 1 else 'MULTIPLE MESHES'}), "
                f"{air_text} air cell(s)."
            )
            final_dc = self.final_conductivity
            final_ip = self.final_chargeability
            lines.append(
                "Final models: "
                f"{final_dc.filename if final_dc else 'none'} (conductivity), "
                f"{final_ip.filename if final_ip else 'none'} (chargeability)."
            )

        lines.append(_verdict_line("Georeference", self.join.unresolved_reasons))
        lines.append(_verdict_line("Mesh", self.mesh.unresolved_reasons))
        return tuple(lines)

    @property
    def processing_notes(self) -> str:
        return "\n".join(self.summary_lines)

    @property
    def anomaly_summary(self) -> str:
        """What the anomaly subgraph reads — the final sections' value ranges.

        Air padding is excluded via ``air_mask`` before any statistic is taken.
        Leaving it in would put ``-1e30`` in a chargeability range and a pinned
        near-zero conductivity in a resistivity one.

        The closing sentence is not decoration. A chargeability high with a
        number on it reads as a drill target, and this one has no location; a
        summary that omits that invites someone to act on a section that cannot
        be placed on a map.
        """
        parts: list[str] = []

        chargeability = self.final_chargeability
        if chargeability is not None:
            earth = chargeability.model.values[~chargeability.model.air_mask]
            parts.append(
                f"Chargeability ({chargeability.filename}, {earth.size} earth cells of "
                f"{chargeability.model.values.size}): {earth.min():.3f}-{earth.max():.3f} mV/V, "
                f"median {float(np.median(earth)):.3f}."
            )

        conductivity = self.final_conductivity
        if conductivity is not None:
            earth = conductivity.model.values[~conductivity.model.air_mask]
            # dcinv2d writes S/m; a geologist reads the section in ohm-m.
            resistivity = 1.0 / earth
            parts.append(
                f"Resistivity ({conductivity.filename}, from S/m): "
                f"{resistivity.min():.1f}-{resistivity.max():.1f} ohm-m, "
                f"median {float(np.median(resistivity)):.1f}."
            )

        if self.is_georeferenced:
            parts.append("Section is georeferenced.")
        else:
            parts.append(
                "Section is in mesh coordinates and is NOT georeferenced — "
                "it cannot be located on a map."
            )
        return " ".join(parts)

    # -- the destination --------------------------------------------------

    def to_geophysics_survey_payload(
        self,
        survey_name: str,
        *,
        contractor: str | None = None,
        acquisition_date: str | None = None,
        interpretation_pdf_id: str | None = None,
    ) -> dict[str, object]:
        """Build the JSON the ``silver_geophysics`` writer already consumes.

        Keys map 1:1 onto ``silver.geophysics_surveys``; see the module
        docstring for why that table and not ``spatial_features``.

        Args:
            survey_name: REQUIRED, and not derived, because it is half of the
                writer's ``ON CONFLICT (workspace_id, survey_name)`` key. A
                default of "L3750N DCIP2D" would silently overwrite one grid's
                survey with another's the first time two grids in one workspace
                both had a line 3750 N — an upsert collision looks like a
                successful ingest from every side.
            contractor: not derived either. The only name in the export is a
                Windows job path inside ``IP.inp``, which records where the
                INVERSION was run.
            acquisition_date: ISO date, and likewise not derived. The parent
                directory is called "June 19" and ``IP.inp``'s run path carries
                a year; a re-run of an old survey would put the wrong year on
                a real acquisition date and nothing downstream could tell.
            interpretation_pdf_id: ``bronze.source_files`` UUID, if a report
                for this line has been ingested.

        Returns:
            The payload dict. ``aoi_wkt`` and ``crs_epsg`` are None whenever the
            join is unresolved, which is the whole point — the reason is spelled
            out in ``processing_notes`` instead, where a person will see it.
        """
        if self.join.resolved and self.join.crs_epsg is not None:
            # Never reached by any delivery in hand. It is logged rather than
            # silently returning None, because a resolved join with a NULL AOI
            # would otherwise look exactly like the unresolved case it is not.
            logger.warning(
                "dcip2d_survey: %s has a RESOLVED station join (EPSG %s) but AOI "
                "construction is not implemented — aoi_geom will be written NULL. "
                "Building it belongs with spatial_parser's CRS handling.",
                self.line_id, self.join.crs_epsg,
            )

        payload: dict[str, object] = {
            "survey_type": SURVEY_TYPE,
            "survey_name": survey_name,
            "contractor": contractor,
            "acquisition_date": acquisition_date,
            "line_ids": [self.line_id],
            "aoi_wkt": None,
            "crs_epsg": self.join.crs_epsg,
            "processing_notes": self.processing_notes,
            "interpretation_pdf_id": interpretation_pdf_id,
            "anomaly_summary": self.anomaly_summary,
        }
        logger.info(
            "dcip2d_survey: payload for '%s' — line %s, %d readings, %d models, "
            "georeferenced=%s",
            survey_name, self.line_id, self.observation_count,
            len(self.models), self.is_georeferenced,
        )
        return payload


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _verdict_line(label: str, reasons: tuple[str, ...]) -> str:
    if not reasons:
        return f"{label}: resolved."
    return f"{label}: NOT RESOLVED — " + "; ".join(reasons) + "."


def _as_float(value: object) -> float | None:
    """Coerce a spreadsheet cell to a float, or None if it is not one.

    ``read_sheet_rows`` hands back every cell of a legacy ``.xls`` as a STRING
    ('4250.0', '404512.845'), so nothing here can be compared to a number until
    it goes through this. That is the trap: ``row["LineNumber"] == 3750`` is
    False for every row of a file that is entirely line 3750, and ``int()`` on
    '4250.0' raises rather than returning 4250.
    """
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        # A control-flow probe, not an error path: the caller decides whether a
        # missing number is fatal for THAT column and logs the rejected row
        # with its index. Logged at debug with the value anyway so that a
        # column which is unparseable in EVERY row is visible — otherwise the
        # only symptom is a station count of zero with nothing explaining it.
        logger.debug("dcip2d_survey: %r is not a number", value)
        return None


def _as_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _canonical_line_id(line_number: float, series: str) -> str:
    number = int(line_number) if float(line_number).is_integer() else line_number
    return f"L{number}{series.upper()}"


def _quantity_from_title(title: str, filename: str) -> str:
    lowered = title.lower()
    for needle, quantity in _QUANTITY_RULES:
        if needle in lowered:
            return quantity
    logger.info(
        "dcip2d_survey: '%s' title %r matches no known quantity — recording '%s'",
        filename, title, QUANTITY_UNKNOWN,
    )
    return QUANTITY_UNKNOWN


def _line_from_dirname(export_dir: Path) -> tuple[float, str] | None:
    """The line a directory is named for, checking the dir then its parent.

    An export lives in ``.../L3750N/export``, so the line name is one level up
    from the directory actually handed in.
    """
    for candidate in (export_dir.name, export_dir.parent.name):
        match = _LINE_IN_DIRNAME.match(candidate.strip())
        if match:
            return float(match.group(1)), match.group(2).upper()
    return None


# ---------------------------------------------------------------------------
# Station file
# ---------------------------------------------------------------------------

def stations_from_rows(
    rows: list[dict[str, object]],
    source: str = "<rows>",
) -> tuple[Dcip2dStation, ...]:
    """Turn already-loaded spreadsheet rows into stations.

    Split from :func:`read_dcip2d_stations` so the station SCHEMA is separable
    from the spreadsheet LOADER. A caller that has already read the sheet — an
    ingestion workflow that classified it, say — should not have to re-open the
    file, and this module's rejection rules are then testable without one.

    A row missing any of :data:`REQUIRED_STATION_COLUMNS` is rejected and
    logged individually with its 1-based row number; it is never dropped
    quietly. A missing ``Z`` is NOT a rejection — see the constant.

    Args:
        rows: One dict per data row, keyed by the sheet's header.
        source: Where the rows came from, for log messages only.

    Returns:
        The accepted stations, in row order.

    Raises:
        ValueError: if *rows* is empty, or if a required COLUMN (as opposed to
            a value) is absent from the header. A sheet with no ``LineNumber``
            column is not a station list at all, and guessing which column
            meant "line" is how a survey gets filed under the wrong one.
    """
    if not rows:
        raise ValueError(f"Station file '{source}' has no rows.")

    missing = [column for column in REQUIRED_STATION_COLUMNS if column not in rows[0]]
    if missing:
        raise ValueError(
            f"Station file '{source}' is missing required column(s) {missing}; "
            f"header is {sorted(rows[0])}."
        )

    stations: list[Dcip2dStation] = []
    for index, row in enumerate(rows, start=1):
        values = {column: _as_float(row.get(column)) for column in REQUIRED_STATION_COLUMNS}
        absent = [column for column, value in values.items() if value is None]
        if absent:
            logger.warning(
                "dcip2d_survey: station file '%s' row %d rejected — "
                "non-numeric or empty %s (raw: %r)",
                source, index, absent, {c: row.get(c) for c in absent},
            )
            continue

        stations.append(
            Dcip2dStation(
                grid_name=_as_text(row.get("Grids_Name")),
                line_number=values["LineNumber"],
                series=_as_text(row.get("Series")),
                station_number=values["StationNumber"],
                station_name=_as_text(row.get("StationName")),
                line_type=_as_text(row.get("LineType")),
                projection_name=_as_text(row.get("Projections_Name")),
                easting=values["X"],
                northing=values["Y"],
                elevation=_as_float(row.get("Z")),
            )
        )

    logger.info(
        "dcip2d_survey: station file '%s' — %d of %d rows accepted, line(s) %s",
        source, len(stations), len(rows),
        sorted({s.line_number for s in stations}),
    )
    return tuple(stations)


def read_dcip2d_stations(path: str | Path) -> tuple[Dcip2dStation, ...]:
    """Read a grid's surveyed IP stations from a Geosoft-style location export.

    Loads through ``xlsx_parser.read_sheet_rows``, which is this package's only
    reader for the legacy BIFF ``.xls`` these arrive as, then hands the rows to
    :func:`stations_from_rows`. Imported inside the function rather than at
    module scope so that reading a DCIP2D export does not drag polars in behind
    it — the same lazy-import contract the rest of this package keeps.

    Raises:
        FileNotFoundError: if *path* does not exist.
        ValueError: as :func:`stations_from_rows`.
    """
    from georag_geoparsers.xlsx_parser import read_sheet_rows  # noqa: PLC0415

    return stations_from_rows(read_sheet_rows(str(path)), str(path))


# ---------------------------------------------------------------------------
# The join
# ---------------------------------------------------------------------------

def _build_join(
    line_number: float,
    chainages: tuple[float, ...],
    station_file: Path | None,
    stations: tuple[Dcip2dStation, ...],
) -> ChainageJoin:
    """Attempt the chainage-to-UTM join and record every reason it did not land."""
    on_line = tuple(s for s in stations if s.line_number == line_number)
    lines_present = tuple(sorted({s.line_number for s in stations}))
    grid_numbers = tuple(sorted({s.station_number for s in stations}))
    projections = tuple(sorted({s.projection_name for s in stations if s.projection_name}))
    matches = tuple(c for c in chainages if c in set(grid_numbers))

    epsg: int | None = None
    for name in projections:
        found = PROJECTION_EPSG.get(name.strip().casefold())
        if found is not None:
            epsg = found
            break

    reasons: list[str] = []

    if station_file is None:
        reasons.append("no station file was supplied")
        return ChainageJoin(
            station_file=None,
            stations_read=0,
            lines_in_station_file=(),
            stations_on_line=(),
            grid_station_numbers=(),
            chainages=chainages,
            exact_matches=(),
            projection_names=(),
            crs_epsg=None,
            unresolved_reasons=tuple(reasons),
        )

    if not on_line:
        reasons.append(
            f"the station file holds no rows for line {line_number:g} — its "
            f"{len(stations)} station(s) are all on line(s) "
            f"{', '.join(f'{n:g}' for n in lines_present) or 'none'}"
        )

    if epsg is None:
        described = ", ".join(repr(p) for p in projections) or "nothing"
        reasons.append(
            f"Projections_Name records {described}, which names no UTM zone and "
            "no datum, so the easting/northing pairs resolve to no EPSG code"
        )

    if chainages and len(matches) != len(chainages):
        spacings = np.diff(np.asarray(chainages)) if len(chainages) > 1 else np.array([])
        spacing_text = (
            f"spacings {spacings.min():.2f}-{spacings.max():.2f} m"
            if spacings.size else "a single position"
        )
        reasons.append(
            f"only {len(matches)} of {len(chainages)} electrode chainages equal a "
            f"station number ({spacing_text} against a picket grid), so placing "
            "them needs the per-line picket-to-metres relation, which is measured "
            "per line and is not in this file"
        )

    return ChainageJoin(
        station_file=str(station_file),
        stations_read=len(stations),
        lines_in_station_file=lines_present,
        stations_on_line=on_line,
        grid_station_numbers=grid_numbers,
        chainages=chainages,
        exact_matches=matches,
        projection_names=projections,
        crs_epsg=epsg,
        unresolved_reasons=tuple(reasons),
    )


def _build_mesh_georeference(export_dir: Path, manifest: dict[str, str]) -> MeshGeoreference:
    """Are the files that place the mesh cells actually in the export?"""
    if not manifest:
        return MeshGeoreference(
            mesh_file=None,
            mesh_delivered=False,
            topography_file=None,
            topography_delivered=False,
            unresolved_reasons=(
                "no .inp control file in the export, so the mesh file is not even named",
            ),
        )

    def _named(label: str) -> str | None:
        value = manifest.get(label, "").strip()
        if not value or value == INP_UNSET:
            return None
        return value

    def _basename(named: str) -> str:
        # The .inp records the path on the machine the inversion ran on — a
        # Windows path with backslashes, which Path() on Linux would treat as
        # one long filename. Only the basename can be looked for here anyway;
        # see read_inp's docstring on why the recorded path is provenance
        # rather than somewhere to go.
        return PurePosixPath(named.replace("\\", "/")).name

    def _delivered(named: str | None) -> bool:
        return named is not None and (export_dir / _basename(named)).exists()

    mesh_file = _named("mesh")
    topography_file = _named("topography")
    mesh_delivered = _delivered(mesh_file)
    topography_delivered = _delivered(topography_file)

    reasons: list[str] = []
    if mesh_file is None:
        reasons.append("the control file names no mesh")
    elif not mesh_delivered:
        reasons.append(
            f"the mesh '{_basename(mesh_file)}' is named in the control file but was "
            "not delivered, so the cells have no width, no depth and no origin"
        )
    if topography_file is not None and not topography_delivered:
        reasons.append(
            f"the topography '{_basename(topography_file)}' is named in the control "
            "file but was not delivered, so the air/ground boundary has no elevation"
        )

    return MeshGeoreference(
        mesh_file=mesh_file,
        mesh_delivered=mesh_delivered,
        topography_file=topography_file,
        topography_delivered=topography_delivered,
        unresolved_reasons=tuple(reasons),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def read_dcip2d_survey(
    export_dir: str | Path,
    *,
    station_file: str | Path | None = None,
) -> Dcip2dSurvey:
    """Assemble one DCIP2D export directory into a persistable survey record.

    Args:
        export_dir: The directory holding the ``.rdt*`` observed data, the
            ``dcinv2d.*`` / ``ipinv2d.*`` models and the ``.inp`` control file.
        station_file: The grid's surveyed station export (``.xls``/``.xlsx``).
            Optional, and deliberately NOT auto-discovered from a sibling
            directory: a station file found by convention and belonging to a
            different grid would produce a confident, wrong georeference, which
            is strictly worse than the honest "no station file was supplied"
            this records instead.

    Returns:
        Dcip2dSurvey. Check ``is_georeferenced`` before treating any position
        in it as a location; ``join.unresolved_reasons`` and
        ``mesh.unresolved_reasons`` say what is missing.

    Raises:
        FileNotFoundError: if *export_dir* is not a directory.
        ValueError: if the export holds no observed-data file; if the splits
            disagree about the array type or the line; if the directory name
            and the file titles name DIFFERENT lines; or if more than one
            ``.inp`` is present and none is named ``IP.inp``. Each of those is
            a delivery whose identity is ambiguous, and a survey row written
            under the wrong line id joins to the wrong stations for good.
    """
    export_dir = Path(export_dir)
    if not export_dir.is_dir():
        raise FileNotFoundError(f"DCIP2D export directory '{export_dir}' does not exist.")

    entries = sorted(p for p in export_dir.iterdir() if p.is_file())

    # -- observed data ----------------------------------------------------
    observed: list[ObservedSplit] = []
    for path in entries:
        if not path.suffix.lower().startswith(_OBSERVED_SUFFIX):
            continue
        data = read_dcip2d_data(path)
        chainages = tuple(sorted({v for record in data.records for v in record[:4]}))
        observed.append(
            ObservedSplit(
                filename=path.name,
                title=data.title,
                array_type=data.array_type,
                quantity=_quantity_from_title(data.title, path.name),
                records=tuple(data.records),
                chainages=chainages,
            )
        )

    if not observed:
        raise ValueError(
            f"DCIP2D export '{export_dir}' holds no observed-data file "
            f"(expected a '{_OBSERVED_SUFFIX}*' suffix). Without one there is no "
            "line identity, no array type and nothing to place."
        )

    array_types = sorted({split.array_type for split in observed})
    if len(array_types) != 1:
        raise ValueError(
            f"DCIP2D export '{export_dir}': observed-data splits disagree about the "
            f"array type {array_types}. The array decides how the four electrode "
            "columns are read, so one export cannot carry two."
        )
    array_type = array_types[0]

    # -- line identity ----------------------------------------------------
    from_titles = {
        (float(m.group(1)), m.group(2).upper())
        for m in (_LINE_IN_TITLE.search(split.title) for split in observed)
        if m
    }
    if len(from_titles) > 1:
        raise ValueError(
            f"DCIP2D export '{export_dir}': observed-data titles name more than one "
            f"line {sorted(from_titles)}."
        )
    from_dirname = _line_from_dirname(export_dir)

    if from_titles:
        line_number, series = from_titles.pop()
        # Only cross-check when the directory looks like a line name at all —
        # an export under a plain 'export' or project folder asserts nothing.
        if from_dirname is not None and from_dirname != (line_number, series):
            raise ValueError(
                f"DCIP2D export '{export_dir}': the directory is named for line "
                f"{_canonical_line_id(*from_dirname)} but the file titles say "
                f"{_canonical_line_id(line_number, series)}. One of them is wrong and "
                "guessing which would file the survey under a line it is not on."
            )
    elif from_dirname is not None:
        line_number, series = from_dirname
        logger.info(
            "dcip2d_survey: no line in any observed-data title — taking line %s "
            "from the directory name",
            _canonical_line_id(line_number, series),
        )
    else:
        raise ValueError(
            f"DCIP2D export '{export_dir}': no line identity in the observed-data "
            "titles and no line in the directory name. line_ids would be empty and "
            "the survey unjoinable to anything."
        )

    # -- models -----------------------------------------------------------
    models: list[ModelFile] = []
    for path in entries:
        match = _MODEL_FILE.match(path.name)
        if not match:
            continue
        family, stage = match.group(1).lower(), match.group(2).lower()
        models.append(
            ModelFile(
                filename=path.name,
                family=family,
                stage=stage,
                iteration=int(stage) if stage.isdigit() else None,
                model=read_dcip2d_model(path),
            )
        )

    # -- control file -----------------------------------------------------
    inp_files = [p for p in entries if p.suffix.lower() == ".inp"]
    if len(inp_files) > 1:
        preferred = [p for p in inp_files if p.name == "IP.inp"]
        if not preferred:
            raise ValueError(
                f"DCIP2D export '{export_dir}' holds {len(inp_files)} control files "
                f"{[p.name for p in inp_files]} and none is named 'IP.inp'; which run "
                "these models came from is ambiguous."
            )
        inp_files = preferred
    manifest = read_inp(inp_files[0]) if inp_files else {}

    # -- the join ---------------------------------------------------------
    chainages = tuple(sorted({c for split in observed for c in split.chainages}))
    stations: tuple[Dcip2dStation, ...] = ()
    station_path = Path(station_file) if station_file is not None else None
    if station_path is not None:
        stations = read_dcip2d_stations(station_path)

    join = _build_join(line_number, chainages, station_path, stations)
    mesh = _build_mesh_georeference(export_dir, manifest)

    survey = Dcip2dSurvey(
        export_dir=export_dir,
        line_id=_canonical_line_id(line_number, series),
        line_number=line_number,
        series=series,
        array_type=array_type,
        observed=tuple(observed),
        models=tuple(models),
        manifest=manifest,
        join=join,
        mesh=mesh,
    )

    logger.info(
        "dcip2d_survey: '%s' — line %s, %d readings across %d split(s), %d model(s), "
        "georeferenced=%s",
        export_dir, survey.line_id, survey.observation_count, len(observed),
        len(models), survey.is_georeferenced,
    )
    if not survey.is_georeferenced:
        logger.warning(
            "dcip2d_survey: '%s' cannot be placed on the ground — %s",
            survey.line_id,
            " | ".join(join.unresolved_reasons + mesh.unresolved_reasons),
        )
    return survey


__all__ = [
    "FINAL_CHARGEABILITY_STAGE",
    "PARSER_NAME",
    "PARSER_VERSION",
    "PROJECTION_EPSG",
    "QUANTITY_UNKNOWN",
    "REQUIRED_STATION_COLUMNS",
    "SURVEY_TYPE",
    "ChainageJoin",
    "Dcip2dStation",
    "Dcip2dSurvey",
    "MeshGeoreference",
    "ModelFile",
    "ObservedSplit",
    "read_dcip2d_stations",
    "read_dcip2d_survey",
    "stations_from_rows",
]
