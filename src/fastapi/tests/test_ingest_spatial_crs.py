"""``_crs_quality`` and ``_crs_epsg`` -- how confident the map is allowed to look.

WHY THIS FILE EXISTS
    ``test_ingest_spatial_archive.py`` covers how a spatial delivery is
    unpacked. Nothing covered what happens to its COORDINATE REFERENCE
    SYSTEM, which is the half that decides whether the features land in
    the right place.

    ``georef_method`` is CHECK-constrained to declared / detected /
    assumed / manual / survey, and the distinction drives the map's
    positional-uncertainty ring. "assumed" is the only honest way to say
    the location may be wrong. A CRS assumed to be WGS84 that is really
    UTM puts a drill collar a continent away -- and if the classifier
    reported that as "declared", the map draws a confident dot with no
    ring, on top of the wrong country.

    So there are two failure modes here, and they are not symmetric.
    Producing a value outside the constraint fails the insert for the
    whole file, which is loud. Producing a value that is INSIDE the
    constraint but too confident is silent, and looks like data.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.hatchet_workflows import ingest_spatial as sp
from app.hatchet_workflows.ingest_spatial import _crs_epsg, _crs_quality

#: The CHECK constraint on silver.spatial_features.georef_method.
LEGAL_METHODS = {"declared", "detected", "assumed", "manual", "survey"}


class TestCrsEpsg:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("EPSG:26913", 26913),
            ("epsg:4326", 4326),
            ("  EPSG:32613  ", 32613),
            (None, None),
            ("", None),
            ("WGS 84", None),
            ("+proj=utm +zone=13 +datum=NAD83", None),
            ("EPSG:", None),
            ("EPSG:not-a-number", None),
        ],
    )
    def test_parses_a_code_or_returns_none(self, source, expected) -> None:
        """None is the correct answer for anything that is not EPSG:<int>.

        Guessing a code from a proj4 string or a datum name would be the
        one mistake with no visible symptom -- the features land somewhere
        plausible and wrong.
        """
        assert _crs_epsg(source) == expected


class TestCrsQuality:
    def test_a_qfield_capture_is_a_survey_fix(self) -> None:
        """QField data comes off a GNSS receiver in someone's hand.

        That is a genuine survey fix, not a guess, and it gets the fixed
        0.9 the pipeline has always assigned it.
        """
        assert _crs_quality(SimpleNamespace(is_qfield=True)) == (0.9, "survey")

    def test_qfield_is_checked_before_the_confidence_score(self) -> None:
        """Order is load-bearing.

        A QField GeoPackage can carry a low parser CRS confidence -- the
        file format says little about the CRS -- while the fix itself is
        good. Reading the score first would demote a real survey to
        "assumed" and draw an uncertainty ring around a GNSS point.
        """
        assert _crs_quality(
            SimpleNamespace(is_qfield=True, crs_confidence=0.1),
        ) == (0.9, "survey")

    def test_missing_confidence_is_assumed_not_declared(self) -> None:
        """The honest default, and the one that raises the ring."""
        assert _crs_quality(SimpleNamespace(crs_confidence=None)) == (
            None, "assumed")

    def test_an_object_with_no_crs_attribute_at_all_is_also_assumed(self) -> None:
        """getattr defaults must not fall through to a confident value."""
        assert _crs_quality(SimpleNamespace()) == (None, "assumed")

    @pytest.mark.parametrize(
        ("confidence", "method"),
        [
            (1.0, "declared"),
            (0.85, "declared"),   # boundary, inclusive
            (0.84, "detected"),
            (0.5, "detected"),    # boundary, inclusive
            (0.49, "assumed"),
            (0.0, "assumed"),
        ],
    )
    def test_the_thresholds_are_inclusive_at_both_boundaries(
        self, confidence: float, method: str,
    ) -> None:
        assert _crs_quality(
            SimpleNamespace(crs_confidence=confidence))[1] == method

    def test_the_confidence_is_passed_through_unchanged(self) -> None:
        """The number reaches crs_confidence on the row; rounding or
        clamping it here would make the ring disagree with the parser."""
        assert _crs_quality(SimpleNamespace(crs_confidence=0.732))[0] == 0.732

    def test_a_string_confidence_is_coerced_not_crashed(self) -> None:
        """Parser results have come back with stringified numbers before,
        and a TypeError here fails the whole file rather than one CRS."""
        assert _crs_quality(SimpleNamespace(crs_confidence="0.9")) == (
            0.9, "declared")

    @pytest.mark.parametrize(
        "result",
        [
            SimpleNamespace(is_qfield=True),
            SimpleNamespace(crs_confidence=None),
            SimpleNamespace(crs_confidence=1.0),
            SimpleNamespace(crs_confidence=0.85),
            SimpleNamespace(crs_confidence=0.6),
            SimpleNamespace(crs_confidence=0.1),
            SimpleNamespace(),
        ],
    )
    def test_every_reachable_method_satisfies_the_check_constraint(
        self, result,
    ) -> None:
        """An illegal value fails the insert for every feature in the file,
        not just the one row -- the same shape as the feature_type bug of
        2026-08-20."""
        assert _crs_quality(result)[1] in LEGAL_METHODS

    def test_manual_is_reachable_only_from_outside_this_function(self) -> None:
        """Documented, not asserted-by-accident.

        "manual" is in the constraint because a geologist can correct a
        CRS in the UI. This classifier never produces it, and should not:
        it only sees what the parser found. If a future change makes it
        return "manual", that is a human claim being invented by a
        heuristic.
        """
        produced = {
            _crs_quality(SimpleNamespace(is_qfield=True))[1],
            _crs_quality(SimpleNamespace(crs_confidence=None))[1],
            _crs_quality(SimpleNamespace(crs_confidence=0.95))[1],
            _crs_quality(SimpleNamespace(crs_confidence=0.6))[1],
            _crs_quality(SimpleNamespace(crs_confidence=0.2))[1],
        }
        assert "manual" not in produced

    def test_the_override_argument_does_not_open_a_back_door(self) -> None:
        """Amended 2026-08-23, deliberately, when 'manual' became reachable.

        It is reachable from ONE place: a source_epsg a person supplied at
        upload time, passed in as an argument. The classifier still cannot
        produce it from the parser's own findings, which is what the test
        above has always been about -- so every call below passes a
        requested code and none of them may earn 'manual' on results the
        request did not decide.
        """
        produced = {
            # a file that declared its own, different CRS
            _crs_quality(
                SimpleNamespace(source_crs="EPSG:32613", crs_confidence=1.0),
                requested_epsg=26904,
            )[1],
            # a QField capture, CRS untouched by the request
            _crs_quality(
                SimpleNamespace(
                    is_qfield=True, source_crs="EPSG:4326", crs_confidence=0.3,
                ),
                requested_epsg=26904,
            )[1],
            # the parser says outright that it did not apply the override
            _crs_quality(
                SimpleNamespace(
                    source_crs="EPSG:26904", crs_confidence=1.0,
                    crs_override_applied=False,
                ),
                requested_epsg=26904,
            )[1],
        }
        assert "manual" not in produced
class TestTheUploaderSuppliedEpsg:
    """``source_epsg`` is the answer to the refusal, so its shape matters.

    A shapefile that arrives without its .prj is now refused rather than
    written at longitude 400,798. That is only defensible because the
    uploader can say what the CRS is -- and only useful if what they say is
    the one thing the rest of the stack already understands: an integer
    EPSG code in the range the database itself enforces.
    """

    WS = "a0000000-0000-0000-0000-00000000feed"
    PROJ = "b1000000-0000-0000-0000-0000000000a0"

    def _input(self, **kw):
        return sp.IngestSpatialInput(
            workspace_id=self.WS, project_id=self.PROJ,
            minio_key="spatial/x/faults.zip", **kw,
        )

    def test_it_defaults_to_none(self) -> None:
        """ingest_zip_archive's spatial fan-out and stale_run_detector's
        recovery both construct this model with three fields. A required
        field here breaks both at validation time, and neither has anything
        to supply -- an override cannot be reconstructed from a bronze key.
        """
        assert self._input().source_epsg is None

    def test_a_real_code_is_accepted(self) -> None:
        """EPSG:26904 -- NAD83 / UTM 4N, the CRS of the Alaska delivery this
        whole change set was measured against."""
        assert self._input(source_epsg=26904).source_epsg == 26904

    @pytest.mark.parametrize("code", [1023, 0, -1, 32768, 999999])
    def test_a_code_outside_the_check_constraint_is_refused(self, code) -> None:
        """crs_epsg_native is CHECK-constrained to 1024..32767.

        Rejecting at the trigger boundary instead of at the INSERT is the
        difference between one 422 the uploader sees and a failure that
        kills every feature in the file long after they have gone -- the
        shape of the feature_type bug of 2026-08-20.
        """
        with pytest.raises(ValidationError):
            self._input(source_epsg=code)

    def test_the_message_states_the_range(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            self._input(source_epsg=999)
        assert "1024-32767" in str(excinfo.value)

    @pytest.mark.parametrize("value", ["EPSG:26904", "NAD83 / UTM zone 4N",
                                       "+proj=utm +zone=4 +datum=NAD83"])
    def test_a_crs_string_is_never_accepted(self, value) -> None:
        """The wire representation is an integer, once, everywhere.

        A string would reach pyproj as free-form text, and the codebase
        already has two spellings of a CRS in the schema
        (projects.crs_epsg INTEGER vs spatial_features.source_crs VARCHAR).
        A third, on the input boundary, is how they drift.
        """
        with pytest.raises(ValidationError):
            self._input(source_epsg=value)

    def test_the_boundaries_themselves_are_inside(self) -> None:
        assert self._input(source_epsg=1024).source_epsg == 1024
        assert self._input(source_epsg=32767).source_epsg == 32767


class TestTheOverrideIsCheckedAgainstTheFileNotTrusted:
    """C3: a CRS the FILE declares always wins.

    An override is a fallback for a file that stated nothing, not a
    correction of one that did. Letting a typed code silently replace a
    declared one is the same corruption this change set closes, running in
    the other direction -- and it would be invisible, because the features
    still land somewhere plausible.
    """

    def test_an_applied_override_is_manual(self) -> None:
        """'manual' is the CHECK constraint's own word for "a person said
        so", and this is the only way to reach it."""
        result = SimpleNamespace(
            source_crs="EPSG:26904", crs_confidence=1.0,
            crs_override_applied=True,
        )
        assert _crs_quality(result, requested_epsg=26904) == (1.0, "manual")

    def test_the_confidence_is_measured_not_asserted(self) -> None:
        """The human's claim is checkable: do these coordinates fall inside
        that CRS's extent? A flat 1.0 would say the map is certain of a
        position nobody verified -- someone picking the wrong UTM zone out
        of a dropdown makes the same mistake the gate exists to catch."""
        result = SimpleNamespace(
            source_crs="EPSG:26904", crs_confidence=0.2,
            crs_override_applied=True,
        )
        assert _crs_quality(result, requested_epsg=26904) == (0.2, "manual")

    def test_a_declared_crs_beats_the_override(self) -> None:
        """The file said 26905; the uploader guessed 26904. The row must
        still read 'declared', on the CRS the file stated."""
        result = SimpleNamespace(source_crs="EPSG:26905", crs_confidence=1.0)
        assert _crs_quality(result, requested_epsg=26904) == (1.0, "declared")

    def test_a_parser_flag_of_false_is_believed(self) -> None:
        """The parser knows which arm of its CRS decision ran; when it says
        so explicitly, that beats inferring from the code that came back."""
        result = SimpleNamespace(
            source_crs="EPSG:26904", crs_confidence=1.0,
            crs_override_applied=False,
        )
        assert _crs_quality(result, requested_epsg=26904)[1] == "declared"

    def test_a_matching_code_with_no_flag_is_still_manual(self) -> None:
        """No flag: the observable fact is that the code that came back is
        the code that went in, which happens either because the parser
        applied it or because the file declared the very same CRS. In the
        second case 'manual' and 'declared' describe identical
        coordinates."""
        result = SimpleNamespace(source_crs="EPSG:26904", crs_confidence=0.95)
        assert _crs_quality(result, requested_epsg=26904)[1] == "manual"

    def test_no_request_means_the_branch_is_unreachable(self) -> None:
        """The reason requested_epsg is a parameter at all: without a human
        in the loop there is nothing for 'manual' to mean."""
        result = SimpleNamespace(
            source_crs="EPSG:26904", crs_confidence=1.0,
            crs_override_applied=True,
        )
        assert _crs_quality(result)[1] == "declared"

    def test_manual_satisfies_the_check_constraint(self) -> None:
        result = SimpleNamespace(
            source_crs="EPSG:32613", crs_confidence=0.7,
            crs_override_applied=True,
        )
        assert _crs_quality(result, requested_epsg=32613)[1] in LEGAL_METHODS

    def test_an_override_outranks_the_qfield_shortcut(self) -> None:
        """Near-hypothetical -- a QField GeoPackage always carries a CRS, so
        the override would not have been applied -- but if the parser says
        it applied one, a person made a claim about this file and a format
        heuristic must not talk over it."""
        result = SimpleNamespace(
            is_qfield=True, source_crs="EPSG:26904", crs_confidence=0.8,
            crs_override_applied=True,
        )
        assert _crs_quality(result, requested_epsg=26904) == (0.8, "manual")


class TestNothingIsWrittenWithoutACrs:
    """The measured corruption, and the only thing that stops it.

    A 4-point shapefile in EPSG:26904, stripped of its .prj, came back
    through this pipeline as POINT (400797.89 6117305.85) -- longitude four
    hundred thousand degrees -- written as SRID 4326 with crs_confidence
    0.0, three warnings attached, and the run reported as finished.

    A status cannot fix that. Laravel's DATA_LANDED_STATUSES is exactly
    ['completed','partial'], so a 'partial' with zero rows still bumps
    data_version and fires the MV refresh. The rows have to not be written.
    """

    def _res(self, crs, **kw):
        return SimpleNamespace(source_crs=crs, **kw)

    def test_a_result_with_no_crs_is_refused(self) -> None:
        refusal = sp._crs_refusal(
            [("faults", self._res(None))], filename="delivery.zip",
        )
        assert refusal is not None
        assert "faults" in refusal

    def test_a_result_with_a_crs_goes_ahead(self) -> None:
        assert sp._crs_refusal(
            [("faults", self._res("EPSG:26904"))], filename="delivery.zip",
        ) is None

    def test_an_empty_string_is_not_a_crs(self) -> None:
        """The parser's own empty-frame path has hard-coded a CRS string in
        the past; an empty one is absence, not a declaration."""
        assert sp._crs_refusal(
            [("faults", self._res(""))], filename="d.zip",
        ) is not None

    def test_the_parser_s_own_verdict_is_honoured(self) -> None:
        """``crs_missing`` is the parser's explicit finding and says why.

        Read alongside the falsy source_crs rather than instead of it: the
        flag is authoritative when present, and the empty string is the
        state the row would actually be written in.
        """
        assert sp._crs_refusal(
            [("faults", self._res("EPSG:4326", crs_missing=True))],
            filename="faults.shp",
        ) is not None

    def test_a_result_that_predates_the_flag_still_passes(self) -> None:
        """Every parse result in these tests is duck-typed, and so is the
        one ingest_zip_archive's fan-out produces on an older worker during
        a rolling deploy."""
        assert sp._crs_refusal(
            [("faults", self._res("EPSG:26904"))], filename="faults.shp",
        ) is None

    def test_one_bad_layer_refuses_the_whole_delivery(self) -> None:
        """The write loop runs inside one transaction, so this is not a
        choice: partial acceptance of a multi-layer delivery would leave
        the good layers committed and the geologist with no way to tell
        which ones landed."""
        assert sp._crs_refusal(
            [
                ("claims", self._res("EPSG:26904")),
                ("faults", self._res(None)),
            ],
            filename="delivery.zip",
        ) is not None

    def test_the_allowlist_is_the_parser_s_job_not_a_second_copy(self) -> None:
        """DXF, DGN and GeoJSON legitimately carry no CRS and return an
        explicit EPSG:4326 for it. This gate therefore needs no format
        list of its own -- and must not grow one, because two lists of
        "formats allowed to have no CRS" is how DXF gets refused by the
        half nobody updated."""
        for crs in ("EPSG:4326",):
            assert sp._crs_refusal(
                [("plan", self._res(crs))], filename="site.dxf",
            ) is None

    def test_the_message_names_the_missing_sidecar(self) -> None:
        """The uploader gets this string and nothing else. "no coordinate
        reference system" is a diagnosis; "no faults.prj" is an
        instruction."""
        refusal = sp._crs_refusal(
            [("faults", self._res(None))],
            filename="delivery.zip",
            sidecars_by_layer={"faults": [".dbf", ".shx"]},
        )
        assert "faults.prj" in refusal

    def test_a_bundle_that_did_ship_a_prj_is_not_told_to_send_one(self) -> None:
        """A mis-cased or unparseable .prj is a different problem, and
        telling someone to include a file they can see in the zip is how a
        support ticket becomes an argument."""
        refusal = sp._crs_refusal(
            [("faults", self._res(None))],
            filename="delivery.zip",
            sidecars_by_layer={"faults": [".dbf", ".prj", ".shx"]},
        )
        assert "faults.prj" not in refusal

    def test_the_message_says_how_to_resolve_it(self) -> None:
        refusal = sp._crs_refusal(
            [(None, self._res(None))], filename="faults.shp",
        )
        assert "source_epsg" in refusal
        assert "1024" in refusal and "32767" in refusal

    def test_an_unnamed_layer_falls_back_to_the_file(self) -> None:
        """A direct upload passes layer_override=None."""
        refusal = sp._crs_refusal(
            [(None, self._res(None))], filename="faults.shp",
        )
        assert "faults.shp" in refusal

    def test_nothing_parsed_is_not_a_refusal(self) -> None:
        """An archive with no readable vector data is already reported as
        'partial' with its own warning; turning it into a CRS failure would
        mis-name the problem."""
        assert sp._crs_refusal([], filename="empty.zip") is None


class TestWarningsReachThePageTheyAreRenderedOn:
    """IngestionRuns.tsx reads ``detail``, then falls back to ``code``.

    The parsers emit ``{code, message, context}``. So every parser warning
    this workflow has ever forwarded -- prj_missing, crs_unknown, and now
    dbf_missing, whose entire job is to say the attribute table did not
    arrive -- rendered as a bare token.
    """

    def test_message_becomes_detail(self) -> None:
        out = sp._renderable([{
            "code": "dbf_missing",
            "message": "no .dbf sidecar for faults.shp; attributes unavailable",
        }])
        assert out[0]["detail"].startswith("no .dbf sidecar")
        assert out[0]["code"] == "dbf_missing"

    def test_an_existing_detail_is_left_alone(self) -> None:
        out = sp._renderable([
            {"code": "x", "message": "m", "detail": "the good one"},
        ])
        assert out[0]["detail"] == "the good one"

    def test_a_warning_with_neither_is_passed_through(self) -> None:
        out = sp._renderable([{"code": "x"}])
        assert out == [{"code": "x"}]

    def test_none_is_an_empty_list(self) -> None:
        assert sp._renderable(None) == []

    def test_the_original_is_not_mutated(self) -> None:
        """The parse result is read again for its features after this."""
        original = {"code": "x", "message": "m"}
        sp._renderable([original])
        assert "detail" not in original
