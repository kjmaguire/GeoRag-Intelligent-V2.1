"""Tier 1 of the nightly sweep could only ever recover PDFs.

It found orphans by LEFT JOINing bronze.manifest against silver.reports —
a table only ingest_pdf writes — and derived a project_id only when the key
started with ``reports/``. Every shapefile, workbook, LAS file and archive
ever uploaded was therefore examined on both the 02:00 and 04:00 passes,
declared an orphan (correctly: spatial never writes silver.reports),
skipped as ``no_project_id``, and noted. Forever. The notes list grew by one
permanent entry per non-PDF upload, drowning the actionable ones, and the
file was never recovered.

The dispatch was hardwired to ingest_pdf besides, so even a correctly
detected geology orphan would have been handed to the PDF parser.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.hatchet_workflows import nightly_ingestion_integrity as nii
from app.hatchet_workflows import stale_run_detector as srd

_WS = "a0000000-0000-0000-0000-00000000feed"
_PJ = "b1000000-0000-0000-0000-0000000000a0"
_RUN = "c2000000-0000-0000-0000-000000000003"

_SRC = Path(nii.__file__).read_text(encoding="utf-8")


def _select_sql() -> str:
    match = re.search(r'select_sql = f"""(.*?)"""', _SRC, re.S)
    assert match, "the Tier 1 SELECT moved — this test needs updating"
    return match.group(1)


class TestOrphanDetectionIsCategoryAware:
    def test_non_pdf_keys_are_not_judged_against_silver_reports(self) -> None:
        """Only ingest_pdf writes silver.reports.

        Judging a shapefile by whether it produced a report row is asking a
        question whose answer is always "no".
        """
        sql = _select_sql()
        assert "silver.ingest_progress" in sql, (
            "non-reports keys must be checked against the ledger every ingest "
            "workflow writes, not against the PDF-only table"
        )
        assert "CASE WHEN split_part(b.file_key, '/', 1) = 'reports'" in sql

    def test_unroutable_prefixes_are_excluded_in_sql(self) -> None:
        """Not merely skipped in Python.

        Skipping in the loop is what appended a permanent note per upload
        per night; excluding them from the SELECT means they are never
        examined at all.
        """
        assert "split_part(b.file_key, '/', 1) = ANY($1::text[])" in _select_sql()

    def test_the_prefix_list_comes_from_the_shared_routing_table(self) -> None:
        """One routing table, two sweeps.

        The stale sweep and the nightly sweep ask the same question — which
        workflow owns this key — and a second hand-maintained copy is how
        the first one drifted.
        """
        assert nii.recoverable_bronze_prefixes is srd.recoverable_bronze_prefixes
        assert nii.recovery_workflow_for_key is srd.recovery_workflow_for_key
        assert "reports" in nii.recoverable_bronze_prefixes()
        assert "spatial" in nii.recoverable_bronze_prefixes()

    def test_the_select_is_valid_postgres(self) -> None:
        """The sweep runs unattended at 02:00; a syntax error is silent."""
        sqlglot = pytest.importorskip(
            "sqlglot", reason="SQL parser not installed in this environment",
        )
        sql = (
            _select_sql()
            .replace("{BRONZE_ORPHAN_AGE_MINUTES}", "30")
            .replace("{BRONZE_MAX_DISPATCH_ATTEMPTS}", "3")
            .replace("$1::text[]", "ARRAY['reports']::text[]")
        )
        sqlglot.parse_one(sql, dialect="postgres")


class TestRecoveryDispatchIsRouted:
    def test_the_dispatcher_targets_the_owning_workflow_s_endpoint(self) -> None:
        assert "_dispatch_recovery" in _SRC
        assert '/internal/v1/shadow/{workflow_name}/trigger' in _SRC
        # The hardwired name is gone.
        assert '"/internal/v1/shadow/ingest_pdf/trigger"' not in _SRC

    @pytest.mark.parametrize(
        ("prefix", "workflow_name"),
        [
            ("reports", "ingest_pdf"),
            ("tiff", "tiff_normalize"),
            ("archive", "ingest_zip_archive"),
            ("spatial", "ingest_spatial"),
            ("well_logs", "ingest_well_logs"),
            ("collars", "ingest_tabular"),
            ("excel", "ingest_tabular"),
        ],
    )
    def test_the_payload_validates_against_the_real_input_model(
        self, prefix: str, workflow_name: str,
    ) -> None:
        """The contract that actually matters at 02:00.

        A field the endpoint's model does not declare — or a required one
        left out — is a 422 in the middle of the night with nobody watching,
        and the sweep records it as `dispatch_returned_none`.
        """
        payload = nii._recovery_trigger_payload(
            workflow_name=workflow_name,
            workspace_id=_WS,
            project_id=_PJ,
            minio_key=f"{prefix}/{_PJ}/upload.bin",
            run_id=_RUN,
            correlation_token="nightly-recovery",
        )
        _, model = srd._build_recovery_payload(
            workflow_name=workflow_name,
            stale_row={
                "workspace_id": _WS,
                "project_id": _PJ,
                "minio_key": f"{prefix}/{_PJ}/upload.bin",
            },
            recovery_run_id=_RUN,
            correlation_token="nightly-recovery",
        )
        # Same model class the trigger endpoint declares; constructing it
        # from the HTTP body is exactly what FastAPI will do.
        rebuilt = type(model)(**payload)
        assert rebuilt.minio_key == f"{prefix}/{_PJ}/upload.bin"

    def test_a_typed_drill_prefix_carries_its_sheet_type(self) -> None:
        payload = nii._recovery_trigger_payload(
            workflow_name="ingest_tabular",
            workspace_id=_WS,
            project_id=_PJ,
            minio_key=f"surveys/{_PJ}/a.csv",
            run_id=_RUN,
            correlation_token="nightly-recovery",
        )
        assert payload["sheet_type"] == "surveys"

    def test_a_workbook_classifies_from_its_own_headers(self) -> None:
        payload = nii._recovery_trigger_payload(
            workflow_name="ingest_tabular",
            workspace_id=_WS,
            project_id=_PJ,
            minio_key=f"excel/{_PJ}/book.xlsx",
            run_id=_RUN,
            correlation_token="nightly-recovery",
        )
        assert "sheet_type" not in payload

    def test_the_run_id_taking_workflows_get_one(self) -> None:
        """So the row the endpoint creates and the row the workflow upserts
        are the same row, rather than two rows for one file."""
        for name in (
            "ingest_zip_archive", "ingest_spatial",
            "ingest_well_logs", "ingest_tabular",
        ):
            payload = nii._recovery_trigger_payload(
                workflow_name=name,
                workspace_id=_WS,
                project_id=_PJ,
                minio_key=f"spatial/{_PJ}/a.gpkg",
                run_id=_RUN,
                correlation_token="nightly-recovery",
            )
            assert payload["run_id"] == _RUN, name


class TestProjectIdIsDerivedForEveryPrefix:
    def test_the_branch_no_longer_special_cases_reports(self) -> None:
        """Every bronze key is `{prefix}/{project_id}/{ts}_{name}`.

        Reading project_id only for `reports` was the second half of why
        nothing else could be recovered, and it survived the first half
        being fixed.
        """
        assert 'parts[0] == "reports"' not in _SRC
        assert "unroutable_key" in _SRC
        # Scoped to emitted notes, not prose: the comment above the branch
        # still names the old `no_project_id` note, which is how anyone
        # reading it learns what changed and why.
        emitted = re.findall(r"report\.notes\.append\(([^)]*)\)", _SRC)
        assert not any("no_project_id" in note for note in emitted)


class TestDispatchTimeProgressRowsExistForEveryWorkflow:
    """The nightly orphan test now leans on ingest_progress being written.

    That only holds if a row appears at DISPATCH time. ingest_pdf and
    tiff_normalize have done it since the Cameco incident; the four geology
    triggers did not, which is why a cancelled geology upload left no trace
    anywhere and could not be told apart from one never dispatched.
    """

    _TRIGGER_SRC = (
        Path(__file__).resolve().parents[1] / "app/routers/shadow_trigger.py"
    ).read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "workflow",
        [
            "ingest_pdf",
            "tiff_normalize",
            "ingest_zip_archive",
            "ingest_spatial",
            "ingest_tabular",
            "ingest_well_logs",
        ],
    )
    def test_each_trigger_records_the_dispatch(self, workflow: str) -> None:
        block = re.search(
            rf"ref = await {workflow}\.aio_run_no_wait\(payload\)(.*?)return ",
            self._TRIGGER_SRC,
            re.S,
        )
        assert block, f"no dispatch block found for {workflow}"
        body = block.group(1)
        assert "_record_dispatch" in body or "start_run" in body, (
            f"{workflow}'s trigger dispatches without creating an "
            "ingest_progress row — a queue-saturation cancellation before the "
            "first task body leaves no trace for the UI, the on_failure hook, "
            "or the nightly sweep."
        )
