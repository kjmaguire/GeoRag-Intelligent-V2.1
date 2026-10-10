"""A recovered ingest run reads the file the way the upload told it to.

The stale sweep and the nightly integrity sweep rebuild a workflow input from
the progress row, and the row carried identity (workspace, project, key) but
nothing the uploader DECLARED. A recovered ingest_tabular run therefore fell
back to ``epsg = input.source_epsg or DEFAULT_SOURCE_EPSG`` (EPSG:32613), so a
collar file declared as EPSG:26904 was re-placed in UTM zone 13 by the very
sweep meant to rescue it; ingest_spatial lost ``source_crs_wkt`` and
``feature_type``, ingest_geophysics its ``source_epsg``, ingest_well_logs its
``hole_id``, and a zip its ``source_epsg`` for every member.

``silver.ingest_progress.dispatch_params`` (migration 2026_10_10_110000) now
records the declared fields when the row is claimed, both builders replay them,
and a run whose parameters were never recorded is declined rather than guessed.

Unit tests need no database; the integration tests need a migrated Postgres.
"""
from __future__ import annotations

import contextlib
import json
import os
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from app.hatchet_workflows import _progress as ingest_progress
from app.hatchet_workflows import nightly_ingestion_integrity as nii
from app.hatchet_workflows import stale_run_detector as srd

WS = "a0000000-0000-0000-0000-00000000feed"
PJ = "b1000000-0000-0000-0000-0000000000a0"
NEW_RUN = "c2000000-0000-0000-0000-000000000051"

COLUMN_MAP = {"collar": {"hole_id": "DDH", "easting": "E_NAD83", "northing": "N_NAD83"}}
WKT = 'PROJCS["NAD83 / UTM zone 4N"]'


def _row(key: str, params: Any = ..., *, step: str = "parse", attempt: int = 1) -> dict:
    row = {
        "run_id": "r1", "workspace_id": WS, "project_id": PJ, "report_id": None,
        "workflow_run_id": None, "minio_key": key, "filename": "x", "current_stage": None,
        "current_step": step, "attempt_number": attempt, "triggered_by": "upload",
    }
    if params is not ...:
        row["dispatch_params"] = params
    return row


def _build(workflow_name: str, stale_row: dict) -> Any:
    return srd._build_recovery_payload(
        workflow_name=workflow_name, stale_row=stale_row,
        recovery_run_id=NEW_RUN, correlation_token="stale-sweep-unit",
    )[1]


# ---------------------------------------------------------------------------
# What is recorded
# ---------------------------------------------------------------------------
def test_the_declared_fields_are_taken_from_the_payload_and_nothing_else() -> None:
    from app.hatchet_workflows.ingest_tabular import IngestTabularInput

    payload = IngestTabularInput(
        workspace_id=WS, project_id=PJ, minio_key=f"collars/{PJ}/a.csv", run_id=NEW_RUN,
        sheet_type="collars", source_epsg=26904, column_map=COLUMN_MAP,
    )
    declared = ingest_progress.dispatch_params_for("ingest_tabular", payload)

    assert declared == {"sheet_type": "collars", "source_epsg": 26904, "column_map": COLUMN_MAP}
    assert "workspace_id" not in declared and "minio_key" not in declared and "run_id" not in declared
    json.dumps(declared)  # storable


def test_an_upload_that_declared_nothing_records_an_empty_object_not_null() -> None:
    """{} says 'recorded: defaults are what it wanted'; NULL says 'unknown'."""
    from app.hatchet_workflows.ingest_tabular import IngestTabularInput

    payload = IngestTabularInput(workspace_id=WS, project_id=PJ, minio_key=f"collars/{PJ}/a.csv")
    assert ingest_progress.dispatch_params_for("ingest_tabular", payload) == {}


def test_a_workflow_with_no_whitelist_records_nothing() -> None:
    assert ingest_progress.dispatch_params_for("something_else", SimpleNamespace(source_epsg=1)) is None


def test_every_whitelisted_field_is_a_real_field_of_its_input_model() -> None:
    """The whitelist is a second list of field names; it must not drift from
    the models the builders construct."""
    from app.hatchet_workflows.ingest_geophysics import IngestGeophysicsInput
    from app.hatchet_workflows.ingest_pdf import IngestPdfInput
    from app.hatchet_workflows.ingest_spatial import IngestSpatialInput
    from app.hatchet_workflows.ingest_tabular import IngestTabularInput
    from app.hatchet_workflows.ingest_well_logs import IngestWellLogsInput
    from app.hatchet_workflows.ingest_zip_archive import IngestZipArchiveInput
    from app.hatchet_workflows.tiff_normalize import TiffNormalizeInput

    models = {
        "ingest_tabular": IngestTabularInput, "ingest_spatial": IngestSpatialInput,
        "ingest_well_logs": IngestWellLogsInput, "ingest_geophysics": IngestGeophysicsInput,
        "ingest_zip_archive": IngestZipArchiveInput, "ingest_pdf": IngestPdfInput,
        "tiff_normalize": TiffNormalizeInput,
    }
    assert set(models) == set(ingest_progress.DISPATCH_PARAM_FIELDS)
    for name, fields in ingest_progress.DISPATCH_PARAM_FIELDS.items():
        assert set(fields) <= set(models[name].model_fields), name


@pytest.mark.parametrize(
    ("raw", "decoded"),
    [(None, None), ("{}", {}), ('{"source_epsg": 26904}', {"source_epsg": 26904}),
     ({"a": 1}, {"a": 1}), ("not json", None), ("[1, 2]", None), (b'{"a": 1}', {"a": 1}), (42, None)],
)
def test_the_column_value_decodes_to_a_dict_or_none(raw: Any, decoded: Any) -> None:
    assert ingest_progress.decode_dispatch_params(raw) == decoded


# ---------------------------------------------------------------------------
# What a sweep may re-dispatch
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "key,workflow",
    [(f"collars/{PJ}/a.csv", "ingest_tabular"), (f"spatial/{PJ}/a.shp", "ingest_spatial"),
     (f"well_logs/{PJ}/a.las", "ingest_well_logs"), (f"xyz/{PJ}/a.xyz", "ingest_geophysics"),
     (f"archive/{PJ}/a.zip", "ingest_zip_archive")],
)
def test_a_run_whose_parameters_were_never_recorded_is_declined(key: str, workflow: str) -> None:
    for unrecorded in (_row(key), _row(key, None), _row(key, "garbage")):
        assert srd.retry_block_reason(unrecorded, max_attempts=3) == f"dispatch_params_unrecorded:{workflow}"


@pytest.mark.parametrize("params", [{}, {"source_epsg": 26904}])
def test_a_run_whose_parameters_were_recorded_is_recovered(params: dict) -> None:
    assert srd.retry_block_reason(_row(f"collars/{PJ}/a.csv", params), max_attempts=3) is None
    assert srd.retry_block_reason(_row(f"collars/{PJ}/a.csv", json.dumps(params)), max_attempts=3) is None


@pytest.mark.parametrize("key", [f"reports/{PJ}/a.pdf", f"tiff/{PJ}/a.tif"])
def test_the_pdf_pair_has_nothing_declared_so_nothing_is_lost(key: str) -> None:
    assert srd.retry_block_reason(_row(key), max_attempts=3) is None


def test_the_other_blocks_keep_their_precedence() -> None:
    assert srd.retry_block_reason(_row(f"collars/{PJ}/a.csv", step="embedding"), max_attempts=3).startswith("embed_stage")
    assert srd.retry_block_reason(_row(f"collars/{PJ}/a.csv", attempt=3), max_attempts=3).startswith("attempts_exhausted")


# ---------------------------------------------------------------------------
# What the stale sweep replays
# ---------------------------------------------------------------------------
def test_tabular_recovery_keeps_the_declared_crs_and_column_map() -> None:
    payload = _build("ingest_tabular", _row(
        f"collars/{PJ}/a.csv", {"source_epsg": 26904, "column_map": COLUMN_MAP},
    ))
    assert payload.source_epsg == 26904
    assert payload.column_map == COLUMN_MAP
    # ... and the upload category is still the sheet_type fallback.
    assert payload.sheet_type == "collars"


def test_a_sheet_type_the_upload_carried_beats_the_prefix() -> None:
    payload = _build("ingest_tabular", _row(f"collars/{PJ}/a.csv", {"sheet_type": "samples"}))
    assert payload.sheet_type == "samples"


def test_spatial_recovery_keeps_crs_wkt_and_feature_type() -> None:
    payload = _build("ingest_spatial", _row(
        f"spatial/{PJ}/a.dxf", {"source_epsg": 26904, "source_crs_wkt": WKT, "feature_type": "fault"},
    ))
    assert (payload.source_epsg, payload.source_crs_wkt, payload.feature_type) == (26904, WKT, "fault")


def test_geophysics_well_logs_and_zip_keep_theirs() -> None:
    geo = _build("ingest_geophysics", _row(f"xyz/{PJ}/a.xyz", {"source_epsg": 26904, "source_name": "Line 7"}))
    assert (geo.source_epsg, geo.source_name) == (26904, "Line 7")
    las = _build("ingest_well_logs", _row(f"well_logs/{PJ}/a.las", {"hole_id": "DDH-12"}))
    assert las.hole_id == "DDH-12"
    zip_ = _build("ingest_zip_archive", _row(f"archive/{PJ}/a.zip", {"source_epsg": 26904}))
    assert zip_.source_epsg == 26904


def test_only_whitelisted_fields_can_come_back_from_the_column() -> None:
    """A value that outlived a renamed field, or a forged one, cannot reach the
    model -- and identity fields can never be overridden through it."""
    payload = _build("ingest_tabular", _row(
        f"collars/{PJ}/a.csv",
        {"source_epsg": 26904, "workspace_id": str(uuid.uuid4()), "run_id": "evil", "bogus": 1},
    ))
    assert payload.workspace_id == WS and payload.run_id == NEW_RUN
    assert payload.source_epsg == 26904


def test_a_row_without_the_column_builds_as_before() -> None:
    """Backwards compatible: callers (and old tests) that pass no params."""
    payload = _build("ingest_tabular", _row(f"collars/{PJ}/a.csv"))
    assert payload.source_epsg is None and payload.column_map is None and payload.sheet_type == "collars"


async def test_the_recovery_row_inherits_the_parameters_so_a_second_recovery_keeps_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_run = AsyncMock(return_value="child-1")
    monkeypatch.setattr(ingest_progress, "start_run", start_run)
    monkeypatch.setattr(ingest_progress, "stamp_workflow_run_id", AsyncMock())
    fake_wf = SimpleNamespace(aio_run_no_wait=AsyncMock(return_value=SimpleNamespace(workflow_run_id="wr")))
    built: dict[str, Any] = {}

    def _fake_build(**kw: Any) -> tuple[Any, Any]:
        built.update(kw)
        return fake_wf, SimpleNamespace(**kw)

    monkeypatch.setattr(srd, "_build_recovery_payload", _fake_build)

    stale = _row(f"collars/{PJ}/a.csv", json.dumps({"source_epsg": 26904}))
    assert await srd._dispatch_recovery_run(stale_row=stale) == "child-1"

    assert start_run.await_args.kwargs["dispatch_params"] == {"source_epsg": 26904}
    assert built["stale_row"]["dispatch_params"] == json.dumps({"source_epsg": 26904})


def test_the_stale_sweep_selects_the_column() -> None:
    import inspect

    assert "dispatch_params" in inspect.getsource(srd.detect.fn if hasattr(srd.detect, "fn") else srd.detect)


# ---------------------------------------------------------------------------
# What the nightly sweep replays
# ---------------------------------------------------------------------------
def test_the_nightly_payload_replays_whitelisted_fields_only() -> None:
    payload = nii._recovery_trigger_payload(
        workflow_name="ingest_tabular", workspace_id=WS, project_id=PJ,
        minio_key=f"collars/{PJ}/a.csv", run_id=NEW_RUN, correlation_token="t",
        dispatch_params={"source_epsg": 26904, "column_map": COLUMN_MAP, "workspace_id": "evil", "x": 1},
    )
    assert payload["source_epsg"] == 26904 and payload["column_map"] == COLUMN_MAP
    assert payload["workspace_id"] == WS and "x" not in payload
    assert payload["sheet_type"] == "collars"


def test_the_nightly_payload_without_params_is_what_it_was() -> None:
    payload = nii._recovery_trigger_payload(
        workflow_name="ingest_tabular", workspace_id=WS, project_id=PJ,
        minio_key=f"excel/{PJ}/a.xlsx", run_id=NEW_RUN, correlation_token="t",
    )
    assert set(payload) == {"workspace_id", "project_id", "minio_key", "run_id"}


@pytest.mark.parametrize(
    ("workflow", "key", "params"),
    [("ingest_tabular", f"collars/{PJ}/a.csv", {"source_epsg": 26904, "column_map": COLUMN_MAP}),
     ("ingest_spatial", f"spatial/{PJ}/a.dxf", {"source_epsg": 26904, "source_crs_wkt": WKT, "feature_type": "fault"}),
     ("ingest_geophysics", f"xyz/{PJ}/a.xyz", {"source_epsg": 26904}),
     ("ingest_well_logs", f"well_logs/{PJ}/a.las", {"hole_id": "DDH-12"}),
     ("ingest_zip_archive", f"archive/{PJ}/a.zip", {"source_epsg": 26904})],
)
def test_the_nightly_payload_validates_against_the_real_model_with_params(
    workflow: str, key: str, params: dict,
) -> None:
    from app.routers import shadow_trigger as st

    payload = nii._recovery_trigger_payload(
        workflow_name=workflow, workspace_id=WS, project_id=PJ, minio_key=key,
        run_id=NEW_RUN, correlation_token="t", dispatch_params=params,
    )
    model = {
        "ingest_tabular": st.IngestTabularInput, "ingest_spatial": st.IngestSpatialInput,
        "ingest_geophysics": st.IngestGeophysicsInput, "ingest_well_logs": st.IngestWellLogsInput,
        "ingest_zip_archive": st.IngestZipArchiveInput,
    }[workflow]
    parsed = model.model_validate(payload)
    for field, value in params.items():
        assert getattr(parsed, field) == value


# ---------------------------------------------------------------------------
# Against Postgres
# ---------------------------------------------------------------------------
pg = pytest.mark.integration


async def _dsn() -> str:
    return ingest_progress._dsn()


@pytest.fixture
async def project():  # noqa: ANN201
    if not os.environ.get("POSTGRES_USER"):
        pytest.skip("postgres env not configured")
    if ingest_progress._pool is not None:
        with contextlib.suppress(Exception):
            await ingest_progress._pool.close()
        ingest_progress._pool = None
    ws, pj = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        conn = await asyncpg.connect(await _dsn(), statement_cache_size=0)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no Postgres: {exc}")
    try:
        has_column = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'silver' "
            "AND table_name = 'ingest_progress' AND column_name = 'dispatch_params')"
        )
        if not has_column:
            pytest.skip("migration 2026_10_10_110000 not applied")
        await conn.execute(
            "INSERT INTO silver.workspaces (workspace_id, name, slug) VALUES ($1::uuid, 'params', $2)",
            ws, f"params-{ws[:8]}",
        )
        await conn.execute(
            "INSERT INTO silver.projects (project_id, project_name, slug, workspace_id, crs_datum, "
            "orientation_reference, status) VALUES ($1::uuid, 'params', $2, $3::uuid, 'EPSG:4326', 'grid', 'active')",
            pj, f"params-{pj[:8]}", ws,
        )
    finally:
        await conn.close()
    try:
        yield ws, pj
    finally:
        if ingest_progress._pool is not None:
            with contextlib.suppress(Exception):
                await ingest_progress._pool.close()
            ingest_progress._pool = None
        conn = await asyncpg.connect(await _dsn(), statement_cache_size=0)
        try:
            await conn.execute("DELETE FROM bronze.manifest WHERE workspace_id = $1::uuid", ws)
            await conn.execute("DELETE FROM silver.ingest_progress WHERE workspace_id = $1::uuid", ws)
            await conn.execute("DELETE FROM silver.projects WHERE project_id = $1::uuid", pj)
            await conn.execute("DELETE FROM silver.workspaces WHERE workspace_id = $1::uuid", ws)
        finally:
            await conn.close()


async def _stored(run_id: str) -> Any:
    conn = await asyncpg.connect(await _dsn(), statement_cache_size=0)
    try:
        return await conn.fetchval(
            "SELECT dispatch_params FROM silver.ingest_progress WHERE run_id = $1::uuid", run_id,
        )
    finally:
        await conn.close()


@pg
async def test_the_trigger_records_what_the_upload_declared_and_the_sweep_replays_it(
    project: tuple[str, str],
) -> None:
    """The reported defect, end to end: a collar upload declared as EPSG:26904
    with a confirmed column map; the run is lost; the stale sweep's rebuilt
    input still says 26904 and still carries the map."""
    from app.hatchet_workflows.ingest_tabular import IngestTabularInput
    from app.routers.shadow_trigger import _claim_and_dispatch

    ws, pj = project
    run_id = str(uuid.uuid4())
    key = f"collars/{pj}/20261010_120000_collars.csv"
    payload = IngestTabularInput(
        workspace_id=ws, project_id=pj, minio_key=key, run_id=run_id,
        sheet_type="collars", source_epsg=26904, column_map=COLUMN_MAP,
    )
    workflow = SimpleNamespace(
        aio_run_no_wait=AsyncMock(return_value=SimpleNamespace(workflow_run_id="wr-1")),
    )

    outcome = await _claim_and_dispatch(workflow, payload, site="ingest_tabular")
    assert outcome.dispatched and outcome.run_id == run_id

    assert json.loads(await _stored(run_id)) == {
        "sheet_type": "collars", "source_epsg": 26904, "column_map": COLUMN_MAP,
    }

    conn = await asyncpg.connect(await _dsn(), statement_cache_size=0)
    try:
        stale_row = dict(await conn.fetchrow(
            "SELECT run_id::text AS run_id, workspace_id::text AS workspace_id, project_id::text AS project_id, "
            "report_id::text AS report_id, workflow_run_id, minio_key, filename, current_stage, current_step, "
            "attempt_number, triggered_by, dispatch_params FROM silver.ingest_progress WHERE run_id = $1::uuid",
            run_id,
        ))
    finally:
        await conn.close()
    assert srd.retry_block_reason(stale_row, max_attempts=3) is None
    recovered = _build("ingest_tabular", stale_row)
    assert recovered.source_epsg == 26904
    assert recovered.column_map == COLUMN_MAP


@pg
async def test_an_upload_that_declared_nothing_is_recorded_as_such(project: tuple[str, str]) -> None:
    from app.hatchet_workflows.ingest_tabular import IngestTabularInput
    from app.routers.shadow_trigger import _claim_and_dispatch

    ws, pj = project
    run_id = str(uuid.uuid4())
    payload = IngestTabularInput(workspace_id=ws, project_id=pj, minio_key=f"collars/{pj}/a.csv", run_id=run_id)
    workflow = SimpleNamespace(
        aio_run_no_wait=AsyncMock(return_value=SimpleNamespace(workflow_run_id="wr-2")),
    )
    await _claim_and_dispatch(workflow, payload, site="ingest_tabular")

    assert json.loads(await _stored(run_id)) == {}


@pg
async def test_a_member_dispatched_by_the_zip_fan_out_records_its_parameters(
    project: tuple[str, str],
) -> None:
    from app.hatchet_workflows.ingest_spatial import IngestSpatialInput
    from app.hatchet_workflows.ingest_zip_archive import IngestZipArchiveInput, _dispatch_member

    ws, pj = project
    archive = IngestZipArchiveInput(
        workspace_id=ws, project_id=pj, minio_key=f"archive/{pj}/a.zip", run_id=str(uuid.uuid4()),
    )
    workflow = SimpleNamespace(
        name="ingest_spatial",
        aio_run_no_wait=AsyncMock(return_value=SimpleNamespace(workflow_run_id="wr-3")),
    )
    payload = IngestSpatialInput(
        workspace_id=ws, project_id=pj, minio_key=f"spatial/{pj}/member.dxf",
        source_epsg=26904, source_crs_wkt=WKT,
    )

    _, progress_run_id = await _dispatch_member(
        workflow, payload, archive=archive, member_name="member.dxf", children=None,
    )

    assert json.loads(await _stored(progress_run_id)) == {"source_epsg": 26904, "source_crs_wkt": WKT}


@pg
async def test_the_claim_a_retry_hits_does_not_overwrite_the_recorded_parameters(
    project: tuple[str, str],
) -> None:
    """A workflow's own start_run upsert (no params) must leave them alone."""
    ws, pj = project
    run_id = str(uuid.uuid4())
    key = f"collars/{pj}/a.csv"
    claim = await ingest_progress.claim_dispatch(
        workspace_id=ws, project_id=pj, minio_key=key, run_id=run_id,
        dispatch_params={"source_epsg": 26904},
    )
    assert claim.claimed
    await ingest_progress.start_run(workspace_id=ws, project_id=pj, minio_key=key, run_id=run_id)

    assert json.loads(await _stored(run_id)) == {"source_epsg": 26904}


@pg
async def test_nightly_tier_1_does_not_guess_the_parameters_of_an_orphan(
    project: tuple[str, str],
) -> None:
    """A non-PDF orphan has no run, so nothing was recorded. Re-dispatching it
    would read a collar file with defaults and -- because the logical source
    name strips the upload timestamp -- replace the rows of a later, correct
    re-upload. It is left alone and said so; a PDF orphan is unaffected."""
    ws, pj = project
    old = "now() - interval '3 hours'"
    conn = await asyncpg.connect(await _dsn(), statement_cache_size=0)
    try:
        for key, doc in (
            (f"collars/{pj}/20261010_100000_collars.csv", "collars"),
            (f"reports/{pj}/20261010_100000_report.pdf", "reports"),
        ):
            await conn.execute(
                f"INSERT INTO bronze.manifest (file_key, workspace_id, sha256, document_type, uploaded_at) "
                f"VALUES ($1, $2::uuid, $3, $4, {old})",
                key, ws, uuid.uuid4().hex * 2, doc,
            )
    finally:
        await conn.close()

    dispatched: list[dict] = []

    async def _fake_dispatch(**kw: Any) -> tuple[str, str]:
        dispatched.append(kw)
        return "dispatched", "run-x"

    pool = await ingest_progress.get_pool()
    with patch.object(nii, "_dispatch_recovery", _fake_dispatch):
        report = await nii._tier_1_bronze(pool)

    mine = [n for n in report.notes if pj in n]
    assert any(n.startswith("dispatch_params_unrecorded:ingest_tabular") for n in mine), mine
    assert [d["workflow_name"] for d in dispatched if d["project_id"] == pj] == ["ingest_pdf"]
