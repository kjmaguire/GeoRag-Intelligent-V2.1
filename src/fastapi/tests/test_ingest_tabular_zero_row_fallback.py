"""A sheet that classified and then wrote nothing was lost in silence.

WHY THIS FILE EXISTS
    ``ingest_tabular``'s text fallback ran under ``if unclassified:``. A
    sheet that DID classify was never in that set, so a sheet the
    classifier accepted and the writer then refused got neither typed
    rows nor searchable text — and the run said nothing about it.

    Measured on the customer's ``export_UTM.xls``: 24 rows of IP
    geophysics station locations (Grids_Name, LineNumber, Series,
    StationNumber, LineType, X, Y, Z). X/Y/Z matched
    easting/northing/elevation, so it classified as 'collar' at 0.75.
    The collar writer then refused every row for having no ``hole_id``.
    Result: ``{'collar': {'written': 0}}``, no passages, and the only
    thing on screen was the unrelated ``xls_legacy_format_detected``
    warning. The reason existed — the parser logged "CSV is missing
    required columns (no alias matched): frozenset({'hole_id'})" — and
    reached nobody but the worker log.

    The second defect is the headline over it. ``rows_written`` counted
    typed silver rows only, so a run whose ONLY output was searchable
    passages reported zero, and IngestionRuns.tsx renders zero as
    "Finished — no data written" — directly above that run's own warning
    saying it indexed N passages.

WHAT THIS FILE DOES NOT COVER
    That the parsers classify these files the way they do. That is
    georag_geoparsers' contract and is measured against the real files;
    here the classification and the parse results are injected, because
    what is under test is what the WORKFLOW does with a zero-row write.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

WS = "a0000000-0000-0000-0000-00000000feed"
PROJECT = "b1000000-0000-0000-0000-0000000000a0"
RUN = "c2000000-0000-0000-0000-000000000003"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

class _SheetMeta:
    """What ``enumerate_sheets`` yields: a name, a guess, a row count."""

    def __init__(
        self, name: str, sheet_type: str, *,
        rows: int = 24, confidence: float = 0.75, hidden: bool = False,
    ) -> None:
        self.name = name
        self.sheet_type = sheet_type
        self.classify_confidence = confidence
        self.row_count = rows
        self.hidden = hidden


class _ParseResult:
    """The shape every CSV parser and ``parse_xlsx_sheet`` returns.

    Only the three fields the workflow reads are modelled. ``records``
    empty plus a file-level ``skipped_details`` entry IS the refusal: it
    is how csv_collar.py:369 reports "no alias matched hole_id".
    """

    def __init__(
        self, *,
        records: list[dict] | None = None,
        warnings: list[dict] | None = None,
        skipped_details: list[dict] | None = None,
    ) -> None:
        self.records = records or []
        self.warnings = warnings or []
        self.skipped_details = skipped_details or []


def _missing_required(columns: str) -> list[dict]:
    """The file-level refusal, verbatim in the parsers' own shape.

    ``row`` is None on the file-level entry and an int on the per-row
    ones, which share the ``missing_required`` code. A reader that looks
    only at the code picks up "row 12 is missing hole_id" instead.
    """
    return [{
        "row": None,
        "code": "missing_required",
        "reason": (
            f"file-level: missing required column mapping(s): "
            f"frozenset({{{columns}}})"
        ),
        "raw": {},
    }]


class _FakeConn:
    """Swallows the writes. The writers are covered by
    test_ingest_tabular_persist.py; here they only need to run."""

    def __init__(self) -> None:
        self.executemany_calls: list[tuple[str, list]] = []
        self.closed = False

    async def executemany(self, sql: str, rows: list) -> None:
        self.executemany_calls.append((sql, list(rows)))

    async def fetch(self, _sql: str, *_args: Any) -> list:
        return []      # _collar_index over an empty silver.collars

    async def fetchval(self, _sql: str, *_args: Any) -> int:
        return 0       # the interval DELETE ... RETURNING count

    def transaction(self):  # noqa: ANN202 — mirrors asyncpg's sync factory
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _txn():
            yield self

        return _txn()

    async def close(self) -> None:
        self.closed = True


class _FakeStore:
    """``get_file`` is the only method the workflow calls."""

    def __init__(self, payload: bytes = b"placeholder") -> None:
        self.payload = payload

    def get_file(self, _bucket: Any, _key: str, local: str) -> None:
        Path(local).write_bytes(self.payload)


class _Env:
    """Every collaborator run_ingest_tabular has, replaced and recording.

    ``sheets`` drives the workbook branch; ``parsed`` maps a sheet label
    to the parse result the workflow will get for it.
    """

    def __init__(self, module: Any) -> None:
        self.it = module
        self.sheets: list[_SheetMeta] = []
        self.parsed: dict[str, _ParseResult] = {}
        self.completed: list[dict] = []
        self.broadcast: list[dict] = []
        self.failed: list[dict] = []
        self.conn = _FakeConn()

    async def run(
        self, filename: str, *, sheet_type: str | None = None,
    ) -> Any:
        payload = self.it.IngestTabularInput(
            workspace_id=WS,
            project_id=PROJECT,
            minio_key=f"bronze/{PROJECT}/{filename}",
            run_id=RUN,
            sheet_type=sheet_type,
        )
        return await self.it.run_ingest_tabular.fn(payload, object())

    # -- assertions the tests share ------------------------------------
    @property
    def warnings(self) -> list[dict]:
        assert self.completed, "the run never reached its terminal write"
        return self.completed[-1]["warnings"]

    def warning(self, code: str) -> dict:
        found = [w for w in self.warnings if w.get("code") == code]
        assert found, (
            f"no {code!r} warning; got "
            f"{[w.get('code') for w in self.warnings]}"
        )
        return found[0]

    @property
    def rows_written(self) -> int:
        assert self.completed, "the run never reached its terminal write"
        return self.completed[-1]["rows_written"]


@pytest.fixture
def env(monkeypatch):
    from app.hatchet_workflows import ingest_tabular as it

    fixture = _Env(it)

    monkeypatch.setattr(it, "get_storage_client", lambda: _FakeStore())
    monkeypatch.setattr(it, "_build_dsn", lambda *a, **kw: "postgres://x/y")

    async def _connect(_dsn):
        return fixture.conn

    monkeypatch.setattr(it.asyncpg, "connect", _connect)

    async def _bind(*a, **kw):
        return None

    monkeypatch.setattr(it, "bind_workspace_scope", _bind)

    async def _start_run(**kw):
        return RUN

    async def _stage(**kw):
        return None

    async def _completed(**kw):
        fixture.completed.append(kw)
        return True

    async def _broadcast(**kw):
        fixture.broadcast.append(kw)

    async def _failed(**kw):
        fixture.failed.append(kw)

    monkeypatch.setattr(it._progress, "start_run", _start_run)
    monkeypatch.setattr(it._progress, "mark_stage_started", _stage)
    monkeypatch.setattr(it._progress, "mark_completed_by_run", _completed)
    monkeypatch.setattr(it._progress, "broadcast_terminal", _broadcast)
    monkeypatch.setattr(it._progress, "mark_failed_by_run", _failed)

    # georag_geoparsers is a path dependency and the workbook branch
    # imports enumerate_sheets from it at call time. Stubbed rather than
    # skipped: the classification is an INPUT to what is under test.
    parser_mod = types.ModuleType("georag_geoparsers.xlsx_parser")
    parser_mod.enumerate_sheets = lambda _path: list(fixture.sheets)
    package = sys.modules.get("georag_geoparsers")
    if package is None:
        package = types.ModuleType("georag_geoparsers")
        package.__path__ = []
        monkeypatch.setitem(sys.modules, "georag_geoparsers", package)
    monkeypatch.setitem(
        sys.modules, "georag_geoparsers.xlsx_parser", parser_mod,
    )

    def _parse_one(path: str, sheet_type: str, sheet_name: str | None) -> Any:
        label = sheet_name or Path(path).name
        assert label in fixture.parsed, (
            f"the workflow parsed {label!r} as {sheet_type!r}, which this "
            f"test did not set up"
        )
        return fixture.parsed[label]

    monkeypatch.setattr(it, "_parse_one", _parse_one)

    return fixture


def _text_fallback(monkeypatch, module, *, passages: int) -> dict:
    """Pin what the text fallback reports, so the count under test is not
    also the count being produced."""
    landed = {
        "code": "unclassified_indexed_as_text",
        "passages": passages,
        "detail": f"indexed as {passages} searchable passage(s)",
    }

    async def _land(*a, **kw):
        return dict(landed)

    monkeypatch.setattr(module, "_land_unclassified_as_text", _land)
    return landed


# ---------------------------------------------------------------------------
# F1 — a classified sheet that wrote nothing falls back AND says why
# ---------------------------------------------------------------------------

class TestAClassifiedSheetThatWroteNothing:
    """export_UTM.xls, reduced to its mechanism."""

    pytestmark = pytest.mark.asyncio

    async def test_it_joins_the_text_fallback(self, env, monkeypatch) -> None:
        """Otherwise the sheet is in the system in no form at all — not
        as drill rows, not as text. That is the silent degradation the
        two spatial PRs before this one existed to kill."""
        env.sheets = [_SheetMeta("export_UTM", "collar", rows=24)]
        env.parsed["export_UTM"] = _ParseResult(
            skipped_details=_missing_required("'hole_id'"),
        )
        sent: dict[str, Any] = {}

        async def _land(_conn, **kw):
            sent.update(kw)
            return {"code": "unclassified_indexed_as_text", "passages": 24,
                    "detail": "indexed"}

        monkeypatch.setattr(env.it, "_land_unclassified_as_text", _land)

        out = await env.run("export_UTM.xls")

        assert sent["unclassified"] == ["export_UTM"]
        assert out.unclassified == ["export_UTM"]

    async def test_the_warning_names_the_sheet_and_its_type(
        self, env, monkeypatch,
    ) -> None:
        env.sheets = [_SheetMeta("export_UTM", "collar", rows=24)]
        env.parsed["export_UTM"] = _ParseResult(
            skipped_details=_missing_required("'hole_id'"),
        )
        _text_fallback(monkeypatch, env.it, passages=24)

        await env.run("export_UTM.xls")
        warning = env.warning("classified_but_nothing_written")

        assert "export_UTM" in warning["detail"]
        assert "collar" in warning["detail"]

    async def test_it_carries_both_message_and_detail(
        self, env, monkeypatch,
    ) -> None:
        """IngestionRuns.tsx:56 renders ``detail``, falls back to
        ``code``, and otherwise shows the geologist a bare token."""
        env.sheets = [_SheetMeta("export_UTM", "collar", rows=24)]
        env.parsed["export_UTM"] = _ParseResult(
            skipped_details=_missing_required("'hole_id'"),
        )
        _text_fallback(monkeypatch, env.it, passages=24)

        await env.run("export_UTM.xls")
        warning = env.warning("classified_but_nothing_written")

        assert warning["message"], "no message key"
        assert warning["detail"], "no detail key"
        assert isinstance(warning["message"], str)
        assert isinstance(warning["detail"], str)

    async def test_the_missing_column_is_in_the_text(
        self, env, monkeypatch,
    ) -> None:
        """Naming the refusal is not enough — "the writer refused it"
        cannot be acted on, "no column matched hole_id" can. The parsers
        already know which columns; the geologist should not have to read
        a worker log to find out."""
        env.sheets = [_SheetMeta("export_UTM", "collar", rows=24)]
        env.parsed["export_UTM"] = _ParseResult(
            skipped_details=_missing_required("'hole_id'"),
        )
        _text_fallback(monkeypatch, env.it, passages=24)

        await env.run("export_UTM.xls")
        warning = env.warning("classified_but_nothing_written")

        assert "hole_id" in warning["detail"]
        assert "frozenset" not in warning["detail"], (
            "a repr reached the user"
        )
        assert "file-level" not in warning["detail"]

    async def test_a_refusal_with_no_stated_reason_still_warns(
        self, env, monkeypatch,
    ) -> None:
        """Nothing may be invented. A parser that returns no records and
        no file-level detail still gets the sheet named and the type
        named; only the specific cause is withheld."""
        env.sheets = [_SheetMeta("Sheet1", "sample", rows=7)]
        env.parsed["Sheet1"] = _ParseResult()
        _text_fallback(monkeypatch, env.it, passages=1)

        await env.run("assays.xlsx")
        warning = env.warning("classified_but_nothing_written")

        assert "Sheet1" in warning["detail"]
        assert "sample" in warning["detail"]
        assert "columns this sheet does not have" in warning["detail"]

    async def test_a_per_row_skip_is_not_read_as_the_file_reason(
        self, env, monkeypatch,
    ) -> None:
        """``missing_required`` tags per-row skips too, and there can be
        thousands. Quoting one of those as the file's reason tells the
        geologist row 12 is the problem when every row is."""
        env.sheets = [_SheetMeta("Collars", "collar", rows=3)]
        env.parsed["Collars"] = _ParseResult(skipped_details=[
            {"row": 12, "code": "missing_required",
             "reason": "row 12: missing required field 'hole_id'"},
        ])
        _text_fallback(monkeypatch, env.it, passages=1)

        await env.run("collars.xlsx")
        warning = env.warning("classified_but_nothing_written")

        assert "row 12" not in warning["detail"]
        assert "columns this sheet does not have" in warning["detail"]

    async def test_a_sheet_that_wrote_rows_is_not_warned_about(
        self, env,
    ) -> None:
        env.sheets = [_SheetMeta("Collars", "collar", rows=2)]
        env.parsed["Collars"] = _ParseResult(records=[
            {"hole_id": "DDH-1", "easting": 500000.0, "northing": 4500000.0},
        ])

        await env.run("collars.xlsx")

        codes = [w.get("code") for w in env.warnings]
        assert "classified_but_nothing_written" not in codes
        assert env.rows_written == 1

    async def test_only_the_refused_sheet_of_two_is_sent_to_text(
        self, env, monkeypatch,
    ) -> None:
        """Scoping is the whole reason the fallback takes a sheet list.
        Sending the workbook would store every collar a second time in
        text shape, competing with the typed row in the recall set."""
        env.sheets = [
            _SheetMeta("Collars", "collar", rows=2),
            _SheetMeta("Stations", "collar", rows=24),
        ]
        env.parsed["Collars"] = _ParseResult(records=[
            {"hole_id": "DDH-1", "easting": 500000.0, "northing": 4500000.0},
        ])
        env.parsed["Stations"] = _ParseResult(
            skipped_details=_missing_required("'hole_id'"),
        )
        sent: dict[str, Any] = {}

        async def _land(_conn, **kw):
            sent.update(kw)
            return {"code": "unclassified_indexed_as_text", "passages": 24,
                    "detail": "indexed"}

        monkeypatch.setattr(env.it, "_land_unclassified_as_text", _land)

        await env.run("mixed.xlsx")

        assert sent["unclassified"] == ["Stations"]

    async def test_the_write_order_is_unchanged(self, env, monkeypatch) -> None:
        """The zero-row set is computed AFTER the write pass. Computing it
        earlier, or reordering the pass to accommodate it, would break
        collars-before-intervals — and every interval FKs to a collar."""
        env.sheets = [
            _SheetMeta("Assays", "sample", rows=5),
            _SheetMeta("Collars", "collar", rows=2),
        ]
        order: list[str] = []

        def _parse_one(path: str, sheet_type: str, sheet_name: str | None):
            order.append(sheet_type)
            return _ParseResult(skipped_details=_missing_required("'hole_id'"))

        monkeypatch.setattr(env.it, "_parse_one", _parse_one)
        _text_fallback(monkeypatch, env.it, passages=2)

        await env.run("mixed.xlsx")

        assert order == ["collar", "sample"]


# ---------------------------------------------------------------------------
# F2 — "no data written" must not be said over landed passages
# ---------------------------------------------------------------------------

class TestPassagesCountAsDataWritten:
    pytestmark = pytest.mark.asyncio

    async def test_a_text_only_run_does_not_report_zero(
        self, env, monkeypatch,
    ) -> None:
        """IngestionRuns.tsx:79 keys the headline on ``rows_written === 0``
        and prints "Finished — no data written". Saying that over a run
        whose own warning reports N indexed passages is the run
        contradicting itself, and the geologist believes the headline."""
        env.sheets = [_SheetMeta("Ages", "unknown", rows=21)]
        _text_fallback(monkeypatch, env.it, passages=21)

        await env.run("Ages from OFR99-136.xls")

        assert env.rows_written == 21

    async def test_typed_rows_and_passages_are_both_counted(
        self, env, monkeypatch,
    ) -> None:
        env.sheets = [
            _SheetMeta("Collars", "collar", rows=2),
            _SheetMeta("Dispatch Log", "unknown", rows=9),
        ]
        env.parsed["Collars"] = _ParseResult(records=[
            {"hole_id": "DDH-1", "easting": 500000.0, "northing": 4500000.0},
            {"hole_id": "DDH-2", "easting": 500100.0, "northing": 4500100.0},
        ])
        _text_fallback(monkeypatch, env.it, passages=9)

        await env.run("mixed.xlsx")

        assert env.rows_written == 11

    async def test_a_run_that_landed_nothing_at_all_still_reports_zero(
        self, env, monkeypatch,
    ) -> None:
        """The count must not become decoration. A fallback that indexed
        nothing adds nothing."""
        env.sheets = [_SheetMeta("Ages", "unknown", rows=21)]

        async def _land(*a, **kw):
            return {"code": "unclassified_not_indexed", "detail": "empty"}

        monkeypatch.setattr(env.it, "_land_unclassified_as_text", _land)

        await env.run("Ages.xlsx")

        assert env.rows_written == 0

    async def test_the_broadcast_and_the_row_agree(
        self, env, monkeypatch,
    ) -> None:
        """Two surfaces read this run: silver.ingest_progress and the
        Reverb toast. A count that reaches one and not the other is the
        same lie in half the places."""
        env.sheets = [_SheetMeta("Ages", "unknown", rows=21)]
        _text_fallback(monkeypatch, env.it, passages=21)

        await env.run("Ages.xls")

        assert env.broadcast, "nothing told Laravel the run finished"
        assert env.broadcast[-1]["status"] == env.it._progress.terminal_status(
            rows_written=env.rows_written, warnings=env.warnings,
        )
        assert "21" in env.broadcast[-1]["message"]

    async def test_a_text_only_run_is_still_partial(
        self, env, monkeypatch,
    ) -> None:
        """The knock-on, stated so it is not mistaken for a regression.
        terminal_status() is 'partial' when rows_written == 0 OR warnings
        exist, and a text-only run always has warnings. Only the headline
        changes."""
        env.sheets = [_SheetMeta("Ages", "unknown", rows=21)]
        _text_fallback(monkeypatch, env.it, passages=21)

        await env.run("Ages.xls")

        assert env.broadcast[-1]["status"] == "partial"


# ---------------------------------------------------------------------------
# The property the fallback must never lose
# ---------------------------------------------------------------------------

class TestTheFallbackCannotFailTheRun:
    pytestmark = pytest.mark.asyncio

    async def test_a_raising_fallback_leaves_the_typed_rows_completed(
        self, env, monkeypatch,
    ) -> None:
        """The typed rows are the valuable part. Letting an openpyxl
        explosion over a dispatch log throw them away inverts the
        priority — and the run would be retried, re-writing them."""
        from app.services.ingest import xlsx_ingester

        env.sheets = [
            _SheetMeta("Collars", "collar", rows=2),
            _SheetMeta("Dispatch Log", "unknown", rows=9),
        ]
        env.parsed["Collars"] = _ParseResult(records=[
            {"hole_id": "DDH-1", "easting": 500000.0, "northing": 4500000.0},
        ])

        async def _boom(*a, **kw):
            raise RuntimeError("openpyxl exploded")

        # The REAL _land_unclassified_as_text, with the ingester under it
        # replaced — the swallow is what is under test.
        monkeypatch.setattr(xlsx_ingester, "ingest_xlsx_file", _boom)

        out = await env.run("mixed.xlsx")

        assert env.failed == [], "the run was marked failed"
        assert env.rows_written == 1
        assert env.warning("unclassified_not_indexed")
        assert "openpyxl exploded" in env.warning(
            "unclassified_not_indexed",
        )["detail"]
        assert out.written["collar"]["written"] == 1

    async def test_a_raising_fallback_does_not_inflate_the_count(
        self, env, monkeypatch,
    ) -> None:
        from app.services.ingest import xlsx_ingester

        env.sheets = [_SheetMeta("Dispatch Log", "unknown", rows=9)]

        async def _boom(*a, **kw):
            raise RuntimeError("openpyxl exploded")

        monkeypatch.setattr(xlsx_ingester, "ingest_xlsx_file", _boom)

        await env.run("notes.xlsx")

        assert env.rows_written == 0

    async def test_the_connection_is_closed_either_way(
        self, env, monkeypatch,
    ) -> None:
        from app.services.ingest import xlsx_ingester

        env.sheets = [_SheetMeta("Dispatch Log", "unknown", rows=9)]

        async def _boom(*a, **kw):
            raise RuntimeError("openpyxl exploded")

        monkeypatch.setattr(xlsx_ingester, "ingest_xlsx_file", _boom)

        await env.run("notes.xlsx")

        assert env.conn.closed


# ---------------------------------------------------------------------------
# The reason extractor, on its own
# ---------------------------------------------------------------------------

class TestTheRefusalReasonIsSourced:
    def test_it_reads_the_parsers_own_words(self) -> None:
        from app.hatchet_workflows import ingest_tabular as it

        result = _ParseResult(
            skipped_details=_missing_required("'hole_id', 'easting'"),
        )
        reason = it._refusal_reason(result)

        assert reason is not None
        assert "hole_id" in reason
        assert "easting" in reason

    def test_it_returns_none_rather_than_guessing(self) -> None:
        from app.hatchet_workflows import ingest_tabular as it

        assert it._refusal_reason(_ParseResult()) is None

    def test_it_ignores_row_level_skips(self) -> None:
        from app.hatchet_workflows import ingest_tabular as it

        result = _ParseResult(skipped_details=[
            {"row": 3, "code": "missing_required", "reason": "row 3: ..."},
        ])

        assert it._refusal_reason(result) is None

    def test_a_result_with_no_skipped_details_attribute_is_survivable(
        self,
    ) -> None:
        """``getattr(..., None)``: the workflow already treats the parse
        result as a duck type for ``records`` and ``warnings``, and a
        parser that grows a different shape must not raise here."""
        from app.hatchet_workflows import ingest_tabular as it

        assert it._refusal_reason(object()) is None

    def test_the_repr_wrapper_is_stripped_but_the_columns_are_not(
        self,
    ) -> None:
        from app.hatchet_workflows import ingest_tabular as it

        cleaned = it._readable_reason(
            "file-level: missing required column mapping(s): "
            "frozenset({'hole_id'})",
        )

        assert cleaned == "missing required column mapping(s): 'hole_id'"


class TestOrphanedIntervalsAreNotTextIndexed:
    """An interval sheet waiting on its collars is not a refused sheet.

    It parsed perfectly well; the holes it references simply have not been
    uploaded yet, and its own orphaned_intervals warning already tells the
    geologist to upload them and re-run. If it were text-indexed now, that
    copy would still be there on the second run when the typed rows finally
    land -- two shapes of the same data competing in the recall set, which is
    the duplication the only_sheets scoping exists to prevent.
    """

    def test_the_zero_write_test_excludes_orphaned_sheets(self):
        import ast
        import inspect

        from app.hatchet_workflows import ingest_tabular as m

        # AST, not a substring grep: the comment right below this condition
        # explains the exclusion and would match a naive text search.
        tree = ast.parse(inspect.getsource(m))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            src = ast.unparse(node.test)
            if "written" in src and "stats" in src:
                found.append(src)
        assert found, "the zero-write condition was not found at all"
        assert any("orphaned" in c for c in found), (
            "the zero-write condition must also exclude orphaned sheets, or an "
            f"interval file awaiting its collars gets text-indexed. Found: {found}"
        )
