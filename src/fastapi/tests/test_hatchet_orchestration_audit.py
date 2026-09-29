"""Regression tests for the 2026-09-29 Hatchet orchestration audit (§07b).

One section per finding. The RLS half of HAT-1 and the database half of
HAT-2/6/7/11 run against a real Postgres as the worker's AWS role in
test_cron_sweeps_under_app_role.py; these are the parts that need no
database.
"""
from __future__ import annotations

import ast
import importlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hatchet_sdk import ConcurrencyLimitStrategy

WORKFLOWS = Path(__file__).resolve().parents[1] / "app" / "hatchet_workflows"


def _as_timedelta(value) -> timedelta:
    if isinstance(value, timedelta):
        return value
    match = re.fullmatch(r"(\d+)([smhd])", str(value).strip())
    assert match, f"unparseable duration {value!r}"
    unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[match.group(2)]
    return timedelta(**{unit: int(match.group(1))})


# ---------------------------------------------------------------------------
# HAT-3 — no ingest workflow relies on Hatchet's 5-minute schedule_timeout
# ---------------------------------------------------------------------------
_INGEST_WORKFLOWS = [
    ("app.hatchet_workflows.ingest_tabular", "ingest_tabular"),
    ("app.hatchet_workflows.ingest_spatial", "ingest_spatial"),
    ("app.hatchet_workflows.ingest_well_logs", "ingest_well_logs"),
    ("app.hatchet_workflows.ingest_zip_archive", "ingest_zip_archive"),
    ("app.hatchet_workflows.promote_silver_to_gold", "promote_silver_to_gold"),
    ("app.hatchet_workflows.ingest_pdf", "ingest_pdf"),
    ("app.hatchet_workflows.tiff_normalize", "tiff_normalize"),
]


@pytest.mark.parametrize(("module", "attr"), _INGEST_WORKFLOWS)
def test_every_ingest_task_can_wait_two_hours_in_the_queue(module, attr) -> None:
    workflow = getattr(importlib.import_module(module), attr)
    # Workflow.tasks includes the on_failure / on_success hooks, which run
    # after the fact and deliberately keep a short schedule_timeout.
    hooks = {
        id(hook) for hook in (
            getattr(workflow, "_on_failure_task", None),
            getattr(workflow, "_on_success_task", None),
        ) if hook is not None
    }
    tasks = [t for t in workflow.tasks if id(t) not in hooks]
    assert tasks, f"{attr}: no tasks found"
    for task in tasks:
        assert _as_timedelta(task.schedule_timeout) >= timedelta(hours=2), (
            f"{attr}.{getattr(task, 'name', task)} keeps a "
            f"{task.schedule_timeout} schedule_timeout; a bulk delivery queues "
            "longer than that behind long tasks and Hatchet silently cancels it"
        )


# ---------------------------------------------------------------------------
# HAT-4 — every ZIP member gets its own progress row and run id
# ---------------------------------------------------------------------------
def test_every_zip_member_dispatch_goes_through_dispatch_member() -> None:
    src = (WORKFLOWS / "ingest_zip_archive.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    calls = re.findall(r"\.aio_run_no_wait\(", code)
    assert len(calls) == 1, (
        "exactly one aio_run_no_wait call is expected in ingest_zip_archive, "
        "the one inside _dispatch_member; a bare dispatch leaves the member "
        "with no progress row (HAT-4)"
    )
    for workflow in ("ingest_tabular", "tiff_normalize", "ingest_pdf", "ingest_spatial"):
        assert re.search(rf"_dispatch_member\(\s*{workflow},", code), workflow


async def test_dispatch_member_records_dispatches_and_stamps(monkeypatch) -> None:
    from app.hatchet_workflows import ingest_zip_archive as zmod
    from app.hatchet_workflows.ingest_pdf import IngestPdfInput
    from app.hatchet_workflows.ingest_tabular import IngestTabularInput

    order: list[str] = []
    started: list[dict] = []

    async def _start_run(**kwargs):
        order.append("start_run")
        started.append(kwargs)
        return kwargs["run_id"]

    async def _stamp(**kwargs):
        order.append("stamp")

    monkeypatch.setattr(zmod.ingest_progress, "start_run", _start_run)
    monkeypatch.setattr(zmod.ingest_progress, "stamp_workflow_run_id", _stamp)

    sent: list = []

    async def _dispatch(payload):
        order.append("dispatch")
        sent.append(payload)
        return SimpleNamespace(workflow_run_id="wf-child")

    workflow = SimpleNamespace(aio_run_no_wait=_dispatch)
    archive = SimpleNamespace(
        workspace_id="a0000000-0000-0000-0000-000000000001",
        project_id="b0000000-0000-0000-0000-000000000001",
        run_id="c0000000-0000-0000-0000-000000000001",
    )
    children: list = []

    _, tab_run = await zmod._dispatch_member(
        workflow,
        IngestTabularInput(
            workspace_id=archive.workspace_id, project_id=archive.project_id,
            minio_key="tabular/p/20260929_120000_000001_a.csv",
        ),
        archive=archive, member_name="a.csv", children=children,
    )
    _, pdf_run = await zmod._dispatch_member(
        workflow,
        IngestPdfInput(
            workspace_id=archive.workspace_id, project_id=archive.project_id,
            minio_key="reports/p/20260929_120000_000001_b.pdf", file_size=1,
            correlation_token="t",
        ),
        archive=archive, member_name="b.pdf", children=children,
    )

    assert order == ["start_run", "dispatch", "stamp"] * 2
    assert sent[0].run_id == tab_run, (
        "a tabular member must carry the run_id its row was written under, so "
        "every attempt upserts the same row instead of minting one per attempt"
    )
    assert [s["run_id"] for s in started] == [tab_run, pdf_run]
    assert [(c.name, c.run_id, c.progress_run_id) for c in children] == [
        ("a.csv", "wf-child", tab_run), ("b.pdf", "wf-child", pdf_run),
    ]


async def test_a_member_dispatch_that_raises_releases_its_row(monkeypatch) -> None:
    from app.hatchet_workflows import ingest_zip_archive as zmod
    from app.hatchet_workflows.ingest_tabular import IngestTabularInput

    release = AsyncMock()
    monkeypatch.setattr(zmod.ingest_progress, "start_run", AsyncMock(return_value="r"))
    monkeypatch.setattr(zmod.ingest_progress, "release_undispatched", release)
    workflow = SimpleNamespace(aio_run_no_wait=AsyncMock(side_effect=RuntimeError("x")))
    ws, pj = "a0000000-0000-0000-0000-000000000001", "b0000000-0000-0000-0000-000000000001"
    archive = SimpleNamespace(workspace_id=ws, project_id=pj, run_id="r0")

    with pytest.raises(RuntimeError):
        await zmod._dispatch_member(
            workflow,
            IngestTabularInput(workspace_id=ws, project_id=pj, minio_key="k"),
            archive=archive, member_name="m", children=[],
        )
    release.assert_awaited_once()


# ---------------------------------------------------------------------------
# HAT-7 — Pass 2 fires, and a dedupe answer is not a failure
# ---------------------------------------------------------------------------
def test_pass_2_is_the_19_utc_tick() -> None:
    from app.hatchet_workflows import nightly_ingestion_integrity as nii

    assert nii._detect_pass_number(datetime(2026, 9, 29, 17, 0, tzinfo=UTC)) == 1
    assert nii._detect_pass_number(datetime(2026, 9, 29, 19, 0, tzinfo=UTC)) == 2
    src = (WORKFLOWS / "nightly_ingestion_integrity.py").read_text(encoding="utf-8")
    crons = re.search(r"on_crons=\[([^\]]*)\]", src)
    assert crons and f'"0 {nii.PASS_2_HOUR_UTC} * * *"' in crons.group(1), (
        "PASS_2_HOUR_UTC must be one of the workflow's own cron hours, or "
        "Pass 2 never runs (it compared against hour 4 for a month)"
    )


class _FakeResponse:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self) -> dict:
        return self._body


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (202, {"workflow_run_id": "wf-1", "dispatched": True}, ("dispatched", "wf-1")),
        (200, {"workflow_run_id": "wf-0", "dispatched": False}, ("already_active", "wf-0")),
        (500, {}, ("failed", None)),
    ],
)
async def test_tier_1_reads_the_trigger_answer(monkeypatch, status, body, expected) -> None:
    from app.hatchet_workflows import nightly_ingestion_integrity as nii

    seen: dict = {}

    class _Client:
        def __init__(self, *a, **kw) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def post(self, url, json, headers):
            seen["headers"] = headers
            return _FakeResponse(status, body)

    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "x" * 40)
    monkeypatch.setattr(nii.httpx, "AsyncClient", _Client)
    result = await nii._dispatch_recovery(
        workflow_name="ingest_pdf",
        workspace_id="a0000000-0000-0000-0000-000000000001",
        project_id="b0000000-0000-0000-0000-000000000001",
        minio_key="reports/b/k.pdf",
    )
    assert result == expected
    assert seen["headers"][nii.INGEST_TRIGGER_HEADER] == "nightly_integrity_sweep"


# ---------------------------------------------------------------------------
# HAT-8 — one promotion per workspace at a time, newest never dropped
# ---------------------------------------------------------------------------
def test_promotions_queue_per_workspace() -> None:
    from app.hatchet_workflows.promote_silver_to_gold import promote_silver_to_gold

    concurrency = promote_silver_to_gold.config.concurrency
    assert concurrency is not None
    assert concurrency.max_runs == 1
    assert concurrency.limit_strategy == ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN, (
        "queue, do not cancel: CANCEL_NEWEST would drop the dispatch that "
        "reads the newest silver"
    )
    referenced = set(re.findall(r"input\.(\w+)", concurrency.expression))
    guarded = set(re.findall(r"has\(\s*input\.(\w+)\s*\)", concurrency.expression))
    assert referenced == {"workspace_id"} and referenced <= guarded


# ---------------------------------------------------------------------------
# HAT-9 — the parse cap scales with page count, bounded
# ---------------------------------------------------------------------------
def test_the_parse_cap_scales_with_pages(monkeypatch) -> None:
    from app.hatchet_workflows import ingest_pdf as mod

    monkeypatch.delenv("PDF_PARSE_SECONDS_PER_PAGE", raising=False)
    assert mod._parse_wall_cap_s(None) == mod.PARSE_WALL_CAP_BASE_S
    assert mod._parse_wall_cap_s(40) == mod.PARSE_WALL_CAP_BASE_S
    big = mod._parse_wall_cap_s(1200)
    assert mod.PARSE_WALL_CAP_BASE_S < big < mod.PARSE_WALL_CAP_MAX_S
    assert mod._parse_wall_cap_s(10**6) == mod.PARSE_WALL_CAP_MAX_S

    monkeypatch.setenv("PDF_PARSE_SECONDS_PER_PAGE", "12")
    assert mod._parse_wall_cap_s(1200) > big
    monkeypatch.setenv("PDF_PARSE_SECONDS_PER_PAGE", "nonsense")
    assert mod._parse_wall_cap_s(1200) == big


def test_the_parse_cap_fires_before_hatchets_timeout() -> None:
    from app.hatchet_workflows import ingest_pdf as mod

    parse_task = next(t for t in mod.ingest_pdf.tasks if "parse" in str(
        getattr(t, "name", "") or getattr(t, "__name__", "")))
    budget = _as_timedelta(parse_task.execution_timeout)
    assert timedelta(seconds=mod.PARSE_WALL_CAP_MAX_S) < budget, (
        "the in-process cap resets the pool; Hatchet's own timeout does not, "
        "so ours has to fire first"
    )


# ---------------------------------------------------------------------------
# HAT-10 — one bad page-image row cannot abort persist's transaction
# ---------------------------------------------------------------------------
def test_the_page_image_insert_runs_in_its_own_savepoint() -> None:
    src = (WORKFLOWS / "ingest_pdf.py").read_text(encoding="utf-8")
    block = re.search(
        r"try:\s*\n\s*async with conn\.transaction\(\):\s*\n\s*_img_status = await conn\.execute\(\s*\n\s*INSERT_IMAGE_PASSAGE_SQL",
        src,
    )
    assert block, (
        "the fail-soft page-image insert must sit inside `async with "
        "conn.transaction()` (a SAVEPOINT): without it one failed row aborts "
        "persist's whole transaction and the document is lost"
    )


# ---------------------------------------------------------------------------
# HAT-11 — a stale in_flight outbox row is reclaimed, a live one is not
# ---------------------------------------------------------------------------
def test_the_reclaim_window_outlives_the_drain() -> None:
    from app.hatchet_workflows import outbox_dispatcher as od

    drain_budget = _as_timedelta(od.drain.execution_timeout)
    assert timedelta(minutes=od.IN_FLIGHT_RECLAIM_MINUTES) > drain_budget, (
        "a row claimed by a drain that is still running must not be reclaimed "
        "from under it"
    )
    assert "status = 'in_flight'" in od._RECLAIM_SQL
    assert "SET status = 'pending'" in od._RECLAIM_SQL


# ---------------------------------------------------------------------------
# HAT-13 — the Neo4j auditor cron is gone
# ---------------------------------------------------------------------------
def test_graph_tenant_audit_is_not_registered() -> None:
    for path in WORKFLOWS.glob("*.py"):
        src = path.read_text(encoding="utf-8")
        assert 'name="graph_tenant_audit"' not in src, (
            f"{path.name} registers graph_tenant_audit, a nightly audit of the "
            "Neo4j store removed 2026-07-28 (CLAUDE.md hard rule 9)"
        )
    # Read the pool tuples rather than importing phase0_agents, whose import
    # chain needs the full Settings environment.
    tree = ast.parse((WORKFLOWS / "phase0_agents.py").read_text(encoding="utf-8"))
    pooled = {
        element.id
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign | ast.Assign)
        for target in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
        if isinstance(target, ast.Name) and target.id.endswith("_AGENT_WORKFLOWS")
        and isinstance(node.value, ast.Tuple)
        for element in node.value.elts
        if isinstance(element, ast.Name)
    }
    assert pooled, "no *_AGENT_WORKFLOWS tuples found in phase0_agents.py"
    assert "graph_tenant_audit" not in pooled
