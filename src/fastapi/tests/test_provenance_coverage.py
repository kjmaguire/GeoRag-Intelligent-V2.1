"""Every module that writes a silver row must have a lineage decision.

WHY THIS FILE EXISTS
    DM-11 reported "the PDF ingest path writes no bronze.provenance rows".
    Measuring it properly turned up something wider: of the thirteen
    modules that write ``INSERT INTO silver.*`` from an ingest path, only
    four write a provenance row. The rest split into "records lineage on
    the silver row instead" and "records none at all", and nothing in the
    codebase distinguished the two -- which is exactly how the gap stayed
    invisible long enough for two UI regressions to be shipped against it
    (SourcesController 2026-08-17, ReportController 2026-08-18, both
    joining bronze.provenance for document rows that never had any).

    The durable fix is not to backfill provenance everywhere -- for the
    document path that would be thousands of rows of derivable data with
    two permanently-NULL tabular columns. It is to make the choice
    EXPLICIT, so the next ingest workflow either records lineage or says
    in writing that it does not, and a reviewer sees which.

HOW IT WORKS
    The lists below are the decision. A module that writes silver rows and
    appears on neither fails this test with instructions. A module on a
    list that stops writing silver rows also fails, so the lists cannot rot
    into a description of code that no longer exists.

    This is a static scan. It proves which SQL a module CONTAINS, not that
    the write executes -- deliberately: the alternative is a live database,
    and the failure this guards against is a whole module having no
    provenance statement at all, which is visible in the text.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

FASTAPI_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = (
    FASTAPI_ROOT / "app" / "hatchet_workflows",
    FASTAPI_ROOT / "app" / "services" / "ingest",
)

_SILVER_INSERT = re.compile(r"INSERT\s+INTO\s+silver\.([a-z_]+)", re.IGNORECASE)

#: Modules that write a bronze.provenance row for what they land.
#: This is the medallion contract as originally designed, and it fits
#: because every one of these parses a ROW-ORIENTED source -- which is
#: what bronze.provenance's source_row / source_col_map columns describe.
WRITES_PROVENANCE: dict[str, str] = {
    "services/ingest/cameco_log_ingester.py":
        "collar rows from a Cameco drill log",
    "services/ingest/csv_collar_ingester.py":
        "collar rows from a CSV. NOTE: zero callers as of 2026-08-21 -- "
        "kept on this list because it still contains the writer, and "
        "deleting it is a separate, already-raised decision",
    "services/ingest/derive_intervals.py":
        "lithology and sample intervals derived from a parsed log",
    "services/ingest/las_ingester.py":
        "collars and curves from a LAS file inside a ZIP",
}

#: Writes bronze.provenance but no silver row, so it is outside the scan's
#: universe and cannot live on the list above. Recorded here because
#: "which code writes provenance" is a question this file should answer
#: completely -- and because leaving it off entirely, after it was on the
#: list above and this file's own staleness check rejected it, would look
#: like it had been forgotten rather than placed.
PROVENANCE_WRITERS_OUTSIDE_SILVER: dict[str, str] = {
    "hatchet_workflows/public_geoscience_pull.py":
        "public survey datasets pulled on a schedule; the rows land in "
        "public_geo.*, and the provenance row points at that schema",
}

#: Modules that record lineage ON the silver row rather than in
#: bronze.provenance. Each entry names the columns that carry it -- if a
#: refactor drops them, the entry is a lie and this file is where someone
#: will look.
LINEAGE_ON_THE_SILVER_ROW: dict[str, str] = {
    "hatchet_workflows/ingest_spatial.py":
        "silver.spatial_features.source_file / source_file_sha256 / "
        "source_layer / source_feature_id / source_crs",
    "hatchet_workflows/ingest_well_logs.py":
        "silver.well_log_curves.source_file / las_version",
    "hatchet_workflows/ingest_pdf.py":
        "silver.reports.source_object_key / source_file_sha256 / "
        "parser_used, plus two audit.audit_ledger actions carrying "
        "minio_key + sha256 + parser_used + trace_id. bronze.provenance "
        "does NOT fit here: source_row and source_col_map have no meaning "
        "for a PDF",
    "services/ingest/pdf_ingester.py":
        "same columns as ingest_pdf -- the service-layer sibling",
    "services/ingest/xlsx_ingester.py":
        "writes silver.reports/document_passages for the text-fallback "
        "path, same lineage columns as the PDF path",
    "services/ingest/raster_metadata.py":
        "silver.raster_layers.source_file_sha256, which is NOT NULL there",
}

#: Modules whose silver writes are not derived from an ingested file at
#: all, so there is no source file to record. Bookkeeping, run state, and
#: values computed from data already in the warehouse.
NOT_FILE_DERIVED: dict[str, str] = {
    "hatchet_workflows/_progress.py":
        "silver.ingest_progress -- run state for the ingest itself",
    "hatchet_workflows/_archive_progress.py":
        "silver.archive_ingest_runs -- run state for an archive",
    "hatchet_workflows/nightly_ingestion_integrity.py":
        "closes stale silver.ingest_progress rows",
    "hatchet_workflows/field_outcome_learning.py":
        "silver.decision_lessons_learned, derived from outcomes",
    "hatchet_workflows/train_source_trust.py":
        "silver.source_trust_scores, computed from corpus statistics",
    "hatchet_workflows/outbox_dispatcher.py":
        "silver.store_reconciliation_findings -- cross-store diffs",
    "services/ingest/cluster_runner.py":
        "silver.projects -- creates the container, parses nothing",
    "hatchet_workflows/nl_summaries.py":
        "ADR-0012 synthesizers. Writes silver.document_passages rows "
        "rendered from silver.assays_v2 / lithology / collars -- no file "
        "is read, so there is no source file to record. The lineage of a "
        "synthesized passage IS its source row, and passage_id is a uuid5 "
        "over '{table}:{row_id}', which makes that link recoverable "
        "without a provenance row. Note this inherits the gap above: a "
        "passage derived from an ingest_tabular collar traces back to a "
        "row that itself has no lineage",
    "hatchet_workflows/promote_silver_to_gold.py":
        "silver.drill_traces -- a desurveyed hole path, computed from "
        "silver.collars + silver.surveys. No file is opened: the geometry "
        "is minimum-curvature arithmetic over rows that are already in "
        "silver, so there is no source_file to record and a provenance "
        "row would name the wrong thing. The lineage IS the collar -- "
        "collar_id is UNIQUE on the table and a FK, so every trace "
        "resolves to exactly one collar and inherits whatever lineage "
        "that collar carries. survey_hash makes the OTHER input "
        "recoverable too: it is a SHA-256 over the ordered stations the "
        "trace was built from, so a trace can be proved stale against the "
        "surveys as they stand now. Inherits the ingest_tabular gap below "
        "for exactly the same reason nl_summaries does -- a trace off a "
        "directly-uploaded collar CSV traces back to a row with no "
        "lineage of its own. The gold.* writes in this module are not "
        "silver and are outside this table's remit; they hang off "
        "collar_id the same way",
}

#: The known gap, recorded rather than hidden. Closing it means adding
#: RETURNING to a batched executemany on the hot ingest path, which wants
#: a live database to verify against.
NO_LINEAGE_YET: dict[str, str] = {
    "hatchet_workflows/ingest_tabular.py":
        "writes silver.collars / surveys / lithology_logs / samples with "
        "no provenance row and no inline source columns. The same drill "
        "file gets lineage when it arrives inside a ZIP "
        "(cameco_log_ingester, derive_intervals) and none when uploaded "
        "directly. Recorded in the bronze.provenance table COMMENT by "
        "migration 2026_08_21_040000",
}

ALL_DECIDED = (
    WRITES_PROVENANCE
    | LINEAGE_ON_THE_SILVER_ROW
    | NOT_FILE_DERIVED
    | NO_LINEAGE_YET
)


def _silver_writers() -> dict[str, list[str]]:
    """Module path (relative to app/) -> the silver tables it inserts into."""
    found: dict[str, list[str]] = {}
    for directory in SCAN_DIRS:
        for path in sorted(directory.rglob("*.py")):
            if path.name.startswith("__"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            tables = sorted(set(_SILVER_INSERT.findall(text)))
            if tables:
                key = path.relative_to(FASTAPI_ROOT / "app").as_posix()
                found[key] = [t.lower() for t in tables]
    return found


def test_the_scan_finds_the_writers_it_is_supposed_to() -> None:
    """Guards the guard: a regex that matches nothing passes everything."""
    writers = _silver_writers()
    assert len(writers) >= 10, (
        f"only {len(writers)} silver writers found -- the scan is probably "
        f"broken rather than the code having changed that much: {writers}"
    )
    assert "hatchet_workflows/ingest_pdf.py" in writers


def test_every_silver_writer_has_an_explicit_lineage_decision() -> None:
    undecided = sorted(set(_silver_writers()) - set(ALL_DECIDED))

    assert not undecided, (
        "These modules write silver rows and are on none of the lists in "
        "this file:\n"
        + "\n".join(f"  - app/{m}" for m in undecided)
        + "\n\nAdd each to exactly one, with a reason:\n"
          "  WRITES_PROVENANCE          it inserts a bronze.provenance row\n"
          "  LINEAGE_ON_THE_SILVER_ROW  the silver row carries source_file "
          "et al\n"
          "  NOT_FILE_DERIVED           nothing was parsed; there is no "
          "source\n"
          "  NO_LINEAGE_YET             a real gap, deliberately open\n\n"
          "Do not pick NO_LINEAGE_YET to make this pass. A silver row a "
          "geologist cannot trace back to the file it came from is the "
          "thing this whole table exists to prevent."
    )


def test_no_list_names_a_module_that_stopped_writing_silver_rows() -> None:
    """Otherwise the lists decay into a description of code that is gone.

    A stale entry is worse than a missing one: it reads as a decision
    somebody made about live code.
    """
    writers = set(_silver_writers())
    stale = sorted(set(ALL_DECIDED) - writers)

    assert not stale, (
        "These modules are listed in this file but no longer contain an "
        "INSERT INTO silver.*:\n"
        + "\n".join(f"  - app/{m}  ({ALL_DECIDED[m]})" for m in stale)
        + "\n\nRemove the entry, or fix the module if the write was lost."
    )


def test_no_module_is_on_two_lists() -> None:
    lists = {
        "WRITES_PROVENANCE": WRITES_PROVENANCE,
        "LINEAGE_ON_THE_SILVER_ROW": LINEAGE_ON_THE_SILVER_ROW,
        "NOT_FILE_DERIVED": NOT_FILE_DERIVED,
        "NO_LINEAGE_YET": NO_LINEAGE_YET,
    }
    seen: dict[str, str] = {}
    clashes = []
    for name, entries in lists.items():
        for module in entries:
            if module in seen:
                clashes.append(f"{module}: {seen[module]} and {name}")
            seen[module] = name

    assert not clashes, "a module can only have one decision:\n" + "\n".join(
        clashes)


@pytest.mark.parametrize(
    "module", sorted(WRITES_PROVENANCE | PROVENANCE_WRITERS_OUTSIDE_SILVER),
)
def test_provenance_writers_really_contain_the_statement(module: str) -> None:
    text = (FASTAPI_ROOT / "app" / module).read_text(encoding="utf-8")
    assert "bronze.provenance" in text, (
        f"app/{module} is on WRITES_PROVENANCE but contains no "
        f"bronze.provenance statement. Either it lost the write, or it "
        f"belongs on another list."
    )


@pytest.mark.parametrize(
    "module", sorted(NOT_FILE_DERIVED | NO_LINEAGE_YET),
)
def test_the_non_writers_really_do_not_write_provenance(module: str) -> None:
    """Symmetry. If one of these grows a provenance write, the entry
    explaining why it has none is now wrong."""
    text = (FASTAPI_ROOT / "app" / module).read_text(encoding="utf-8")
    assert "INSERT INTO bronze.provenance" not in text, (
        f"app/{module} now writes bronze.provenance but is listed as not "
        f"doing so. Move it to WRITES_PROVENANCE."
    )


def test_the_spatial_lineage_columns_named_in_the_list_are_really_bound() -> None:
    """The LINEAGE_ON_THE_SILVER_ROW entries make a factual claim.

    ingest_spatial's entry claims source_file_sha256 is populated. It was
    not until 2026-08-21 -- the column existed, carried an index and a
    COMMENT pointing at bronze.ingest_manifest, and the INSERT never bound
    it. An entry on that list asserting a column nobody writes is exactly
    the failure mode this file was built to stop.
    """
    text = (
        FASTAPI_ROOT / "app" / "hatchet_workflows" / "ingest_spatial.py"
    ).read_text(encoding="utf-8")

    insert = text.split("_INSERT_SQL", 1)[1].split('"""', 2)[1]
    for column in (
        "source_file", "source_file_sha256", "source_layer",
        "source_feature_id", "source_crs",
    ):
        assert column in insert, (
            f"{column} is named in LINEAGE_ON_THE_SILVER_ROW but is not in "
            f"ingest_spatial's INSERT"
        )


def test_the_well_log_lineage_columns_are_really_bound() -> None:
    text = (
        FASTAPI_ROOT / "app" / "hatchet_workflows" / "ingest_well_logs.py"
    ).read_text(encoding="utf-8")

    insert = text.split("_CURVE_SQL", 1)[1].split('"""', 2)[1]
    for column in ("source_file", "las_version"):
        assert column in insert, (
            f"{column} is named in LINEAGE_ON_THE_SILVER_ROW but is not in "
            f"ingest_well_logs' INSERT"
        )


def test_the_report_lineage_columns_are_really_bound() -> None:
    text = (
        FASTAPI_ROOT / "app" / "hatchet_workflows" / "ingest_pdf.py"
    ).read_text(encoding="utf-8")

    insert = text.split("INSERT_REPORT_SQL", 1)[1].split('"""', 2)[1]
    for column in ("source_file_sha256", "source_object_key", "parser_used"):
        assert column in insert, (
            f"{column} is named in LINEAGE_ON_THE_SILVER_ROW but is not in "
            f"ingest_pdf's report INSERT"
        )
