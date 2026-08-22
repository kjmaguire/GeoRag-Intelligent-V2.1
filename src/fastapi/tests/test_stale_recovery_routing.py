"""The stale sweep must re-run the workflow that owns the file.

Every ingest workflow writes the same three stage names ("preflight",
"parse", "persist") into ``silver.ingest_progress``, and the row does not
record which workflow wrote them. The sweep used to read a stale row at one
of those stages and unconditionally dispatch ``ingest_pdf`` — so a
GeoPackage that wedged in GDAL was "recovered" by handing it to the PDF
parser, which downloaded it, found no ``%PDF-`` magic and failed the
recovery run with ``preflight_rejected: missing %PDF- magic bytes``. Two
failed rows, the second carrying a reason with nothing to do with the user's
file, and the upload never retried by the workflow that could read it.

These are unit tests: no Postgres, no Hatchet dispatch. The live-DB
behaviour of the sweep loop stays in ``test_stale_run_detector.py`` (which
is ``integration``-marked and therefore does not run in the PR bucket — one
more reason the routing itself is checked here).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.hatchet_workflows import stale_run_detector as srd

_WS = "a0000000-0000-0000-0000-00000000feed"
_PJ = "b1000000-0000-0000-0000-0000000000a0"

#: Repo root, from src/fastapi/tests/ → ../../..
_REPO = Path(__file__).resolve().parents[3]
_UPLOAD_CONTROLLER = _REPO / "app/Http/Controllers/Api/V1/UploadController.php"


def _row(key: str) -> dict:
    return {
        "workspace_id": _WS,
        "project_id": _PJ,
        "minio_key": key,
        "run_id": "c2000000-0000-0000-0000-000000000009",
    }


class TestKeyPrefixRouting:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("reports/{p}/20260821_120000_ni43101.pdf", "ingest_pdf"),
            ("tiff/{p}/20260821_120000_scan.tif", "tiff_normalize"),
            ("archive/{p}/20260821_120000_field.zip", "ingest_zip_archive"),
            ("spatial/{p}/20260821_120000_faults.gpkg", "ingest_spatial"),
            ("well_logs/{p}/20260821_120000_hole.las", "ingest_well_logs"),
            ("collars/{p}/20260821_120000_collars.csv", "ingest_tabular"),
            ("surveys/{p}/20260821_120000_surveys.csv", "ingest_tabular"),
            ("lithology/{p}/20260821_120000_litho.csv", "ingest_tabular"),
            ("samples/{p}/20260821_120000_assays.csv", "ingest_tabular"),
            ("excel/{p}/20260821_120000_book.xlsx", "ingest_tabular"),
            ("tabular/{p}/20260821_120000_from_zip.csv", "ingest_tabular"),
        ],
    )
    def test_every_live_bronze_prefix_routes_to_its_own_workflow(
        self, key: str, expected: str,
    ) -> None:
        assert srd.recovery_workflow_for_key(key.format(p=_PJ)) == expected

    def test_the_geopackage_that_started_this_no_longer_goes_to_the_pdf_parser(
        self,
    ) -> None:
        """The exact failure from the audit, as a regression test."""
        key = f"spatial/{_PJ}/20260821_120000_corrupt.gpkg"
        assert srd.recovery_workflow_for_key(key) != "ingest_pdf"
        assert srd.recovery_workflow_for_key(key) == "ingest_spatial"

    @pytest.mark.parametrize(
        "key",
        [
            # A retired category. Its parser has no workflow, so there is
            # nothing to re-run and pretending otherwise manufactures a
            # second failure.
            "geophysics/{p}/x.json",
            "seismic/{p}/x.segy",
            # Not a bronze key shape at all.
            "just_a_loose_file.pdf",
            "",
        ],
    )
    def test_an_unroutable_key_gets_no_recovery_rather_than_a_guess(
        self, key: str,
    ) -> None:
        assert srd.recovery_workflow_for_key(key.format(p=_PJ)) is None

    def test_a_missing_key_is_unroutable_not_an_exception(self) -> None:
        # `minio_key` is nullable on ingest_progress and the sweep reads
        # rows it did not write.
        assert srd.recovery_workflow_for_key(None) is None

    def test_a_bucket_qualified_key_still_routes(self) -> None:
        # Some call sites carry the bucket name in the key.
        assert (
            srd.recovery_workflow_for_key(f"bronze/spatial/{_PJ}/x.gpkg")
            == "ingest_spatial"
        )


class TestRecoveryPayloads:
    """Each route must build the input model its workflow actually declares."""

    @pytest.mark.parametrize(
        ("key", "workflow_name"),
        [
            (f"reports/{_PJ}/a.pdf", "ingest_pdf"),
            (f"tiff/{_PJ}/a.tif", "tiff_normalize"),
            (f"archive/{_PJ}/a.zip", "ingest_zip_archive"),
            (f"spatial/{_PJ}/a.gpkg", "ingest_spatial"),
            (f"well_logs/{_PJ}/a.las", "ingest_well_logs"),
            (f"collars/{_PJ}/a.csv", "ingest_tabular"),
        ],
    )
    def test_builder_returns_the_named_workflow_and_a_valid_input(
        self, key: str, workflow_name: str,
    ) -> None:
        workflow, payload = srd._build_recovery_payload(
            workflow_name=workflow_name,
            stale_row=_row(key),
            recovery_run_id="c2000000-0000-0000-0000-000000000001",
            correlation_token="stale-sweep-unit",
        )
        # The router's string and the registered Hatchet workflow name are
        # the same value — if they ever drift, the dispatch log line names a
        # workflow that is not the one that ran.
        assert workflow.name == workflow_name
        assert payload.minio_key == key
        assert str(payload.workspace_id) == _WS
        assert str(payload.project_id) == _PJ

    @pytest.mark.parametrize(
        ("prefix", "expected_sheet_type"),
        [
            ("collars", "collars"),
            ("surveys", "surveys"),
            ("lithology", "lithology"),
            ("samples", "samples"),
            # A workbook classifies every sheet on its own, and `tabular/`
            # is written by the ZIP extractor for a file nobody typed. Both
            # must classify from the header rather than inherit a hint.
            ("excel", None),
            ("tabular", None),
        ],
    )
    def test_the_typed_drill_prefix_is_passed_back_as_the_sheet_type_hint(
        self, prefix: str, expected_sheet_type: str | None,
    ) -> None:
        """The prefix IS the hint the geologist chose at upload time.

        A CSV whose headers do not self-identify only routed correctly the
        first time because of that hint; dropping it on the retry would
        misclassify the file on the attempt that was supposed to rescue it.
        """
        _, payload = srd._build_recovery_payload(
            workflow_name="ingest_tabular",
            stale_row=_row(f"{prefix}/{_PJ}/a.csv"),
            recovery_run_id="c2000000-0000-0000-0000-000000000001",
            correlation_token="stale-sweep-unit",
        )
        assert payload.sheet_type == expected_sheet_type

    @pytest.mark.parametrize(
        ("key", "workflow_name"),
        [
            (f"archive/{_PJ}/a.zip", "ingest_zip_archive"),
            (f"spatial/{_PJ}/a.gpkg", "ingest_spatial"),
            (f"well_logs/{_PJ}/a.las", "ingest_well_logs"),
            (f"collars/{_PJ}/a.csv", "ingest_tabular"),
        ],
    )
    def test_run_id_taking_workflows_receive_the_reserved_recovery_row(
        self, key: str, workflow_name: str,
    ) -> None:
        """These four upsert their progress row under the id we hand them.

        ingest_pdf and tiff_normalize are the other shape: they take no
        run_id and adopt the row via lookup_active_run_id — asserted below.
        """
        run_id = "c2000000-0000-0000-0000-000000000042"
        _, payload = srd._build_recovery_payload(
            workflow_name=workflow_name,
            stale_row=_row(key),
            recovery_run_id=run_id,
            correlation_token="stale-sweep-unit",
        )
        assert payload.run_id == run_id

    @pytest.mark.parametrize(
        ("key", "workflow_name"),
        [
            (f"reports/{_PJ}/a.pdf", "ingest_pdf"),
            (f"tiff/{_PJ}/a.tif", "tiff_normalize"),
        ],
    )
    def test_the_lookup_shaped_workflows_carry_the_correlation_token(
        self, key: str, workflow_name: str,
    ) -> None:
        _, payload = srd._build_recovery_payload(
            workflow_name=workflow_name,
            stale_row=_row(key),
            recovery_run_id="c2000000-0000-0000-0000-000000000001",
            correlation_token="stale-sweep-unit",
        )
        assert not hasattr(payload, "run_id")
        assert payload.correlation_token == "stale-sweep-unit"
        # file_size is informational: preflight re-derives the real size
        # against the 2 GB cap from the bytes it downloads.
        assert payload.file_size == 0

    def test_an_unbuilt_workflow_name_raises_rather_than_silently_skipping(
        self,
    ) -> None:
        """A prefix added to the map without a builder must be loud.

        Returning None here would look exactly like "this key is not
        recoverable", and the run would never be retried with no sign why.
        """
        with pytest.raises(ValueError, match="no recovery payload builder"):
            srd._build_recovery_payload(
                workflow_name="ingest_something_new",
                stale_row=_row(f"newthing/{_PJ}/a.bin"),
                recovery_run_id="c2000000-0000-0000-0000-000000000001",
                correlation_token="stale-sweep-unit",
            )

    def test_every_routed_workflow_name_has_a_builder(self) -> None:
        """Lockstep between the prefix map and the builder's branches."""
        for prefix, workflow_name in srd._RECOVERY_WORKFLOW_BY_PREFIX.items():
            workflow, _ = srd._build_recovery_payload(
                workflow_name=workflow_name,
                stale_row=_row(f"{prefix}/{_PJ}/a.bin"),
                recovery_run_id="c2000000-0000-0000-0000-000000000001",
                correlation_token="stale-sweep-unit",
            )
            assert workflow.name == workflow_name, prefix


class TestLockstepWithLaravelsUploadPrefixes:
    """The prefix map is a second copy of a list Laravel owns.

    ``UploadController::bronzePrefixes()`` is CATEGORIES' keys plus ``tiff``
    and ``tabular``. A category restored there without a matching entry here
    silently loses stale-run recovery for that whole file type — which is
    precisely the failure mode this finding is about, one layer up.
    """

    def _laravel_live_prefixes(self) -> set[str]:
        src = _UPLOAD_CONTROLLER.read_text(encoding="utf-8")
        block = re.search(
            r"private const CATEGORIES = \[(.*?)^    \];",
            src,
            re.S | re.M,
        )
        assert block, "CATEGORIES block not found — did the controller move?"
        keys = set(re.findall(r"^\s*'([a-z_]+)' => \[", block.group(1), re.M))
        assert keys, "parsed zero categories — the regex needs updating"
        # The two prefixes that are not category names; see bronzePrefixes().
        return keys | {"tiff", "tabular"}

    @pytest.mark.skipif(
        not _UPLOAD_CONTROLLER.exists(),
        reason="Laravel tree not present (fastapi-only checkout)",
    )
    def test_every_live_upload_prefix_can_be_recovered(self) -> None:
        missing = self._laravel_live_prefixes() - set(
            srd._RECOVERY_WORKFLOW_BY_PREFIX
        )
        assert not missing, (
            f"UploadController accepts {sorted(missing)} but the stale sweep "
            "has no recovery workflow for them — uploads of those types will "
            "be marked timed_out and never retried."
        )

    @pytest.mark.skipif(
        not _UPLOAD_CONTROLLER.exists(),
        reason="Laravel tree not present (fastapi-only checkout)",
    )
    def test_the_map_does_not_route_prefixes_laravel_no_longer_mints(
        self,
    ) -> None:
        extra = set(srd._RECOVERY_WORKFLOW_BY_PREFIX) - self._laravel_live_prefixes()
        assert not extra, (
            f"the stale sweep routes {sorted(extra)}, which UploadController "
            "no longer accepts — a retired category re-dispatching is how the "
            "sweep manufactures failures nobody can explain."
        )
