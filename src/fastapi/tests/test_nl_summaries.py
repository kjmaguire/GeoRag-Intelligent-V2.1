"""ADR-0012's synthesizers, ported out of the dormant Dagster tree.

WHAT THIS COVERS AND WHAT IT CANNOT
    The renderers are pure functions of one fetched row, so every sentence
    they produce is testable against literal dicts -- which is most of the
    risk, because these strings ARE the retrieval surface. A missing
    sample ID or a swallowed QA/QC flag is not a formatting bug; it is the
    reason the question cannot be answered.

    The fetch SQL is NOT tested here. It needs a live Postgres and belongs
    in the integration bucket. Every column it reads was verified against
    the migrations before porting -- including ``assays_v2.instrument``,
    which the Dagster asset predates by three days, and the five collar
    columns from the 2026-05-20 drillhole extension -- but "the column
    exists" is not "the query returns what I think".

WHY THE PASSAGE ID TESTS MATTER MOST
    ``passage_id`` is a uuid5 over ``{table}:{row_id}``. If it stops being
    derived from the source row, a re-run stops being an UPDATE and starts
    duplicating the entire structured corpus -- silently, because nothing
    downstream distinguishes two passages about one assay from two assays.
"""
from __future__ import annotations

import uuid

import pytest

from app.hatchet_workflows.nl_summaries import (
    CHUNK_KIND_STRUCTURED,
    PARSER_USED,
    SOURCES,
    SYNTHESIZERS,
    NlSummariesInput,
    build_rows,
    derive_passage_id,
    format_element,
    render_assay_passage,
    render_collar_passage,
    render_lithology_passage,
    text_hash,
)

WS = "a0000000-0000-0000-0000-00000000feed"


def assay_row(**overrides):
    row = {
        "representative_id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
        "workspace_id": WS,
        "sample_id": "MS-240301",
        "from_depth": 118.5,
        "to_depth": 119.5,
        "hole_id": "PLS-22-08",
        "project_name": "Patterson Lake",
        "analysis_method": "ICP-MS",
        "lab_name": "SGS Lakefield",
        "certificate_ref": "CA-2024-0771",
        "instrument": "Agilent 7900",
        "qaqc_flag": "pass",
        "rock_code": "GNS",
        "rock_name": "graphitic pelitic gneiss",
        "elements": {
            "U3O8": {"value": 1.84, "unit": "wt%"},
            "Mo": {"value": 12, "unit": "ppm", "under_detection": True},
        },
    }
    row.update(overrides)
    return row


def lithology_row(**overrides):
    row = {
        "id": uuid.UUID("22222222-2222-2222-2222-222222222222"),
        "workspace_id": WS,
        "from_depth": 0.0,
        "to_depth": 42.0,
        "hole_id": "PLS-22-08",
        "project_name": "Patterson Lake",
        "rock_code": "SST",
        "rock_name": "Athabasca sandstone",
        "colour": "buff",
        "grain_size": "medium",
        "logged_by": "R. Okafor",
        "logged_date": "2024-03-01",
    }
    row.update(overrides)
    return row


def collar_row(**overrides):
    row = {
        "collar_id": uuid.UUID("33333333-3333-3333-3333-333333333333"),
        "workspace_id": WS,
        "hole_id": "PLS-22-08",
        "project_name": "Patterson Lake",
        "easting": 495123.4,
        "northing": 6421987.0,
        "elevation": 512.0,
        "total_depth": 421.5,
        "hole_type": "DDH",
        "azimuth": 135.0,
        "dip": -62.0,
        "drill_date": "2024-02-14",
        "hole_status": "completed",
        "purpose": "resource definition",
        "driller": "Boart Longyear",
        "geologist": "R. Okafor",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Element formatting
# ---------------------------------------------------------------------------

class TestFormatElement:
    def test_a_plain_result(self) -> None:
        assert format_element("U3O8", {"value": 1.84, "unit": "wt%"}) == (
            "U3O8 1.84 wt%")

    def test_below_detection_is_said_out_loud(self) -> None:
        """"0.001 ppm" and "below detection at 0.001 ppm" mean different
        things to a geologist deciding whether an element is absent or
        merely unmeasured, and the difference has to survive into the text
        a retriever matches on."""
        assert "(below detection)" in format_element(
            "Mo", {"value": 12, "unit": "ppm", "under_detection": True})

    def test_above_detection_is_said_out_loud(self) -> None:
        assert "(above detection)" in format_element(
            "U3O8", {"value": 20, "unit": "wt%", "over_detection": True})

    def test_a_null_value_is_nd_not_zero(self) -> None:
        """Rendering a missing assay as 0 would put a false negative into
        the corpus, which is worse than saying nothing."""
        assert format_element("Au", {"value": None, "unit": "ppm"}) == "Au ND"

    def test_a_missing_unit_does_not_leave_a_dangling_space(self) -> None:
        assert format_element("Au", {"value": 3}) == "Au 3"


# ---------------------------------------------------------------------------
# Assay passages
# ---------------------------------------------------------------------------

class TestRenderAssayPassage:
    def test_it_names_everything_a_question_might_ask_for(self) -> None:
        """This is the passage the ADR exists for. The question it has to
        answer is "which holes returned above 1% U3O8 and what QA/QC flags
        were on those samples?", so every one of those tokens must be in
        the text."""
        text = render_assay_passage(assay_row())

        for token in ("MS-240301", "PLS-22-08", "Patterson Lake",
                      "118.5", "119.5", "U3O8", "1.84", "wt%",
                      "ICP-MS", "SGS Lakefield", "CA-2024-0771",
                      "Agilent 7900", "QA/QC: pass"):
            assert token in text, f"{token!r} missing from: {text}"

    def test_elements_are_ordered_deterministically(self) -> None:
        """Two runs over unchanged data must produce the same text, or the
        upsert nulls embedding_id and re-embeds the whole corpus for
        nothing."""
        first = render_assay_passage(assay_row())
        second = render_assay_passage(assay_row())
        assert first == second
        assert first.index("Mo ") < first.index("U3O8 "), "sorted by element"

    def test_host_rock_is_included_when_the_lateral_join_found_one(
        self,
    ) -> None:
        assert "Host rock at interval: graphitic pelitic gneiss." in (
            render_assay_passage(assay_row()))

    def test_it_falls_back_to_the_rock_code_when_there_is_no_name(
        self,
    ) -> None:
        text = render_assay_passage(assay_row(rock_name=None))
        assert "Host rock at interval: GNS." in text

    def test_no_host_rock_means_no_empty_clause(self) -> None:
        text = render_assay_passage(assay_row(rock_name=None, rock_code=None))
        assert "Host rock" not in text

    def test_an_unknown_qaqc_flag_is_stated_not_omitted(self) -> None:
        """Silence about QA/QC reads as "it passed". It has to say it does
        not know."""
        assert "QA/QC: unknown." in render_assay_passage(
            assay_row(qaqc_flag=None))

    def test_missing_hole_and_project_are_marked_not_blank(self) -> None:
        text = render_assay_passage(
            assay_row(hole_id=None, project_name=None))
        assert "(unknown hole)" in text
        assert "(unknown project)" in text

    def test_an_empty_element_map_still_renders(self) -> None:
        """An assay row with no element aggregate should not raise; it is
        a data problem to surface, not a crash."""
        text = render_assay_passage(assay_row(elements={}))
        assert "MS-240301" in text


# ---------------------------------------------------------------------------
# Lithology passages
# ---------------------------------------------------------------------------

class TestRenderLithologyPassage:
    def test_it_names_the_interval_and_the_rock(self) -> None:
        text = render_lithology_passage(lithology_row())
        for token in ("PLS-22-08", "Patterson Lake", "0.0", "42.0",
                      "Athabasca sandstone", "rock code SST"):
            assert token in text, f"{token!r} missing from: {text}"

    def test_the_code_is_not_repeated_when_it_equals_the_name(self) -> None:
        text = render_lithology_passage(
            lithology_row(rock_code="SST", rock_name="SST"))
        assert "rock code" not in text

    def test_attributes_are_listed_only_when_present(self) -> None:
        with_attrs = render_lithology_passage(lithology_row())
        assert "Attributes: colour buff, grain size medium." in with_attrs

        without = render_lithology_passage(
            lithology_row(colour=None, grain_size=None))
        assert "Attributes:" not in without

    def test_a_long_description_is_truncated(self) -> None:
        """A free-text log entry can run to a page, and a passage that is
        95% one geologist's prose retrieves as prose -- which the PDF
        corpus already covers."""
        text = render_lithology_passage(
            lithology_row(description="x" * 400))
        assert "…" in text
        assert "x" * 281 not in text

    def test_a_short_description_is_left_alone(self) -> None:
        text = render_lithology_passage(
            lithology_row(description="strongly hematised, brittle"))
        assert "Description: strongly hematised, brittle" in text
        assert "…" not in text

    def test_the_logger_and_date_travel_together(self) -> None:
        assert "Logged by R. Okafor on 2024-03-01." in (
            render_lithology_passage(lithology_row()))

    def test_a_logger_with_no_date_still_renders(self) -> None:
        assert "Logged by R. Okafor." in render_lithology_passage(
            lithology_row(logged_date=None))

    def test_neither_rock_code_nor_name_says_so(self) -> None:
        text = render_lithology_passage(
            lithology_row(rock_code=None, rock_name=None))
        assert "unspecified rock type" in text


# ---------------------------------------------------------------------------
# Collar passages
# ---------------------------------------------------------------------------

class TestRenderCollarPassage:
    def test_it_names_the_hole_its_geometry_and_its_crew(self) -> None:
        text = render_collar_passage(collar_row())
        for token in ("PLS-22-08", "Patterson Lake", "DDH",
                      "495123.4", "6421987.0", "512.0",
                      "Azimuth 135.0°, dip -62.0°", "Total depth 421.5 m",
                      "Drilled 2024-02-14", "Status: completed",
                      "Purpose: resource definition",
                      "drilled by Boart Longyear", "logged by R. Okafor"):
            assert token in text, f"{token!r} missing from: {text}"

    def test_a_dip_of_zero_is_still_reported(self) -> None:
        """A horizontal hole is a real thing, and 0 is falsy. Testing
        `if dip` instead of `if dip is not None` would drop it."""
        assert "dip 0" in render_collar_passage(collar_row(dip=0.0))

    def test_an_elevation_of_zero_is_still_reported(self) -> None:
        assert "elevation 0.0 m" in render_collar_passage(
            collar_row(elevation=0.0))

    def test_azimuth_alone_renders_without_the_dip_clause(self) -> None:
        text = render_collar_passage(collar_row(dip=None))
        assert "Azimuth 135.0°." in text
        assert "dip" not in text.lower().split("azimuth")[1][:20]

    def test_no_orientation_at_all_leaves_no_empty_clause(self) -> None:
        text = render_collar_passage(collar_row(azimuth=None, dip=None))
        assert "Azimuth" not in text
        assert "Dip" not in text

    def test_hole_status_wins_over_status(self) -> None:
        """The two columns coexist; hole_status is the drillhole-specific
        one added by the 2026-05-20 extension."""
        text = render_collar_passage(
            collar_row(hole_status="abandoned", status="active"))
        assert "Status: abandoned." in text

    def test_it_falls_back_to_status_when_hole_status_is_absent(self) -> None:
        text = render_collar_passage(
            collar_row(hole_status=None, status="active"))
        assert "Status: active." in text

    def test_drill_type_stands_in_for_a_missing_hole_type(self) -> None:
        text = render_collar_passage(
            collar_row(hole_type=None, drill_type="RC"))
        assert "type RC." in text


# ---------------------------------------------------------------------------
# Passage identity — the part that makes a re-run an update
# ---------------------------------------------------------------------------

class TestPassageIdentity:
    def test_the_same_source_row_always_derives_the_same_id(self) -> None:
        row_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        assert derive_passage_id("silver.assays_v2", row_id) == (
            derive_passage_id("silver.assays_v2", row_id))

    def test_different_tables_never_collide(self) -> None:
        row_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        assert derive_passage_id("silver.assays_v2", row_id) != (
            derive_passage_id("silver.lithology", row_id))

    def test_it_is_a_valid_uuid(self) -> None:
        uuid.UUID(derive_passage_id("silver.collars", "abc"))

    def test_the_hash_is_exactly_the_column_width(self) -> None:
        """document_passages.text_hash is CHAR(64) with a
        `^[0-9a-f]{64}$` CHECK. A shorter hex string is padded by the
        column and fails the check."""
        digest = text_hash("anything")
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)


class TestBuildRows:
    def test_it_produces_the_upsert_tuple_shape(self) -> None:
        rows = build_rows("collars", [collar_row()], WS)

        assert len(rows) == 1
        passage_id, workspace_id, text, digest, kind, parser = rows[0]
        uuid.UUID(passage_id)
        assert workspace_id == WS
        assert "PLS-22-08" in text
        assert digest == text_hash(text)
        assert kind == CHUNK_KIND_STRUCTURED
        assert parser == PARSER_USED

    def test_the_row_s_own_workspace_wins_over_the_input(self) -> None:
        """The fetch is already workspace-scoped, so these agree -- but if
        they ever diverge the ROW is the authority. Writing another
        tenant's row under this run's workspace is the one mistake with no
        recovery."""
        other = "b1000000-0000-0000-0000-0000000000a0"
        rows = build_rows("collars", [collar_row(workspace_id=other)], WS)
        assert rows[0][1] == other

    def test_a_row_with_no_id_is_skipped_not_given_a_random_one(self) -> None:
        """A passage whose id is not derived from its source row would be
        re-created on every run, so the corpus would grow without bound."""
        rows = build_rows(
            "lithology",
            [lithology_row(), lithology_row(id=None)],
            WS,
        )
        assert len(rows) == 1

    def test_every_declared_source_can_build(self) -> None:
        """Guards the SYNTHESIZERS table: a wrong id column name would
        silently skip every row rather than raise."""
        fixtures = {
            "assays": assay_row(),
            "lithology": lithology_row(),
            "collars": collar_row(),
        }
        for source in SOURCES:
            rows = build_rows(source, [fixtures[source]], WS)
            assert len(rows) == 1, f"{source} built no rows"

    def test_the_synthesizer_table_and_sources_agree(self) -> None:
        assert set(SOURCES) == set(SYNTHESIZERS)


# ---------------------------------------------------------------------------
# Input contract
# ---------------------------------------------------------------------------

class TestInput:
    def test_a_workspace_is_required(self) -> None:
        """There is no fan-out shape and no cron here, so no payload
        legitimately omits it -- and a synthesis run that guessed its
        tenant would write another workspace's data into this corpus."""
        with pytest.raises(ValueError):
            NlSummariesInput()

    def test_a_malformed_workspace_is_rejected_at_the_boundary(self) -> None:
        with pytest.raises(ValueError):
            NlSummariesInput(workspace_id="not-a-uuid")

    def test_it_defaults_to_every_source(self) -> None:
        assert NlSummariesInput(workspace_id=WS).sources == list(SOURCES)

    def test_an_unknown_source_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown sources"):
            NlSummariesInput(workspace_id=WS, sources=["assays", "structures"])

    def test_an_empty_source_list_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="cannot be empty"):
            NlSummariesInput(workspace_id=WS, sources=[])

    def test_dry_run_is_off_by_default(self) -> None:
        assert NlSummariesInput(workspace_id=WS).dry_run is False


class TestWorkflowRegistration:
    def test_it_has_no_cron(self) -> None:
        """The first run over an existing corpus writes one passage per
        structured row and embeds every one of them. That is an operator
        decision, not something a schedule should start."""
        from app.hatchet_workflows.nl_summaries import nl_summaries

        assert not getattr(nl_summaries, "on_crons", None)

    def test_the_worker_registers_it(self) -> None:
        """Registered but unscheduled is the intended state; unregistered
        would mean it cannot be triggered at all."""
        from pathlib import Path

        worker = (
            Path(__file__).resolve().parent.parent
            / "app" / "hatchet_workflows" / "worker.py"
        ).read_text(encoding="utf-8")

        assert "from app.hatchet_workflows.nl_summaries import nl_summaries" in worker
        assert "\n        nl_summaries,\n" in worker

    def test_the_chunk_kind_is_not_narrative(self) -> None:
        """ingest_pdf's garbage collector deletes by
        (document_id, chunk_kind='narrative', stale hash). A synthesized
        passage carrying that kind would be deleted by the next PDF
        re-parse."""
        assert CHUNK_KIND_STRUCTURED != "narrative"
        assert CHUNK_KIND_STRUCTURED == "structured_summary"
