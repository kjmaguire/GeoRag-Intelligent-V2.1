"""The read-only project diagnostics run (scripts/ops/project_data_diagnostics.py).

No AWS, no database: Postgres is a scripted fake connection, scope-aware the way RLS is
for the project lookup. The pure helpers are tested directly; each check is run against
the fake, including the table-absent, column-absent and slug-not-found paths.

The SQL itself was additionally exercised against a real PostgreSQL 16 + PostGIS 3
scratch database (fail-closed RLS on the workspace-scoped tables, read-only session, a
non-owner role) when the script was written; that is not part of this suite.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ops"))

import project_data_diagnostics as pdd  # noqa: E402

_WS_A = "a0000000-0000-0000-0000-000000000001"
_WS_B = "b0000000-0000-0000-0000-000000000002"
_PID = "11111111-1111-1111-1111-111111111111"

ALL_TABLES = {
    "silver.projects",
    "silver.ingest_progress",
    "silver.collars",
    "silver.surveys",
    "silver.lithology_logs",
    "silver.lithology",
    "silver.structure",
    "silver.well_log_curves",
    "silver.samples",
    "silver.drill_traces",
    "gold.drillhole_intervals_visual",
    "gold.structure_measurements_visual",
    "silver.attribute_tables",
    "silver.spatial_features",
    "silver.archive_ingest_runs",
}


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


# ---------------------------------------------------------------------------
# A scripted connection
# ---------------------------------------------------------------------------


class FakeConn:
    """Answers by rule: the first ``(matcher, answer)`` whose matcher (a substring of the
    whitespace-normalised SQL, or a predicate on it) fits wins. ``answer`` is a value, an
    Exception (raised), or a callable ``(sql, args) -> value``. The project lookup by slug
    is scope-aware: ``projects_by_scope`` maps the bound workspace (None = unscoped) to
    the row that scope can see."""

    def __init__(
        self,
        *,
        projects_by_scope: dict[str | None, dict | None] | None = None,
        workspaces: tuple[str, ...] = (),
        tables: set[str] | None = None,
        rules: list[tuple[Any, Any]] | None = None,
    ) -> None:
        self.projects_by_scope = projects_by_scope or {}
        self.workspaces = list(workspaces)
        self.tables = set(ALL_TABLES if tables is None else tables)
        self.rules = list(rules or [])
        self.scope: str | None = None
        self.sql: list[str] = []
        self.args: list[tuple] = []

    def _answer(self, sql: str, args: tuple, default: Any) -> Any:
        norm = _norm(sql)
        for matcher, answer in self.rules:
            hit = matcher(norm) if callable(matcher) else matcher in norm
            if hit:
                if isinstance(answer, Exception):
                    raise answer
                return answer(sql, args) if callable(answer) else answer
        return default

    async def execute(self, sql, *args):
        self.sql.append(sql)
        self.args.append(args)
        if "set_config('app.workspace_id'" in sql:
            self.scope = (args[0] if args else "") or None

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        self.args.append(args)
        if "WHERE p.slug = $1" in sql:
            return self.projects_by_scope.get(self.scope)
        return self._answer(sql, args, None)

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        self.args.append(args)
        if "silver.workspaces" in sql:
            return [{"workspace_id": w} for w in self.workspaces]
        return self._answer(sql, args, [])

    async def fetchval(self, sql, *args):
        self.sql.append(sql)
        self.args.append(args)
        if "to_regclass" in sql:
            return args[0] in self.tables
        return self._answer(sql, args, 0)

    def is_in_transaction(self) -> bool:
        return False

    async def close(self) -> None:
        return None


def _project_row(ws: str | None = _WS_B, slug: str = "red-star") -> dict:
    return {"project_id": _PID, "workspace_id": ws, "slug": slug}


def _project() -> pdd.Project:
    return pdd.Project(project_id=_PID, workspace_id=_WS_B, slug="red-star")


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestWarningCodes:
    def test_codes_only_never_detail(self) -> None:
        raw = json.dumps([{"code": "orphaned_intervals", "detail": "CUSTOMER TEXT"}, {"code": "las_no_depth"}])
        assert pdd.warning_codes(raw) == ["orphaned_intervals", "las_no_depth"]

    def test_decoded_value_and_single_dict(self) -> None:
        assert pdd.warning_codes([{"code": "rows_rejected"}]) == ["rows_rejected"]
        assert pdd.warning_codes({"code": "dbf_no_rows"}) == ["dbf_no_rows"]

    def test_missing_code_and_bare_strings(self) -> None:
        assert pdd.warning_codes([{"detail": "x"}, "a bare string", 7]) == ["(no code)"] * 3

    def test_free_text_in_the_code_slot_is_not_echoed(self) -> None:
        out = pdd.warning_codes([{"code": "the customer said something with spaces"}])
        assert out == ["(non-code text)"]

    def test_empty_and_garbage(self) -> None:
        assert pdd.warning_codes(None) == []
        assert pdd.warning_codes("[]") == []
        assert pdd.warning_codes("not json") == ["(unparseable warnings)"]
        assert pdd.warning_codes(42) == []

    def test_watch_list(self) -> None:
        assert pdd.is_watched("orphaned_intervals")
        assert pdd.is_watched("las_anything_at_all")
        assert not pdd.is_watched("collar_crs_assumed")


class TestScrubError:
    def test_none_passes_through(self) -> None:
        assert pdd.scrub_error(None) is None

    def test_truncates_at_300(self) -> None:
        out = pdd.scrub_error("x" * 1000)
        assert out is not None and out.startswith("x" * 300) and out.endswith("...[truncated]")
        assert len(out) == 300 + len("...[truncated]")

    def test_collapses_whitespace(self) -> None:
        assert pdd.scrub_error("a\n\n  b\tc") == "a b c"

    def test_connection_string_credentials_are_removed(self) -> None:
        out = pdd.scrub_error("could not connect postgresql://georag:hunter2@db.internal:5432/x")
        assert out is not None and "hunter2" not in out and "[REDACTED]" in out


class TestMarkdown:
    def test_pipes_and_newlines_cannot_break_a_table(self) -> None:
        lines = pdd.md_table(["a", "b"], [("x|y", "l1\nl2")])
        assert lines[2] == "| x\\|y | l1 l2 |"

    def test_errored_paths_walks_nested_sub_checks(self) -> None:
        checks = {
            "a": {"status": "ok", "sub": {"status": "error", "error": "boom"}},
            "b": {"status": "error", "error": "x"},
            "c": {"status": "table_absent", "missing_tables": ["silver.x"]},
        }
        assert sorted(pdd.errored_paths(checks)) == ["a.sub", "b"]


class TestArgs:
    def test_slug_is_required(self, capsys) -> None:
        with pytest.raises(SystemExit) as exc:
            pdd.build_parser().parse_args([])
        assert exc.value.code == 2
        assert "--project-slug" in capsys.readouterr().err

    @pytest.mark.parametrize("bad", ["", "Red-Star", "red star", "red_star", "a" * 65, "x;drop", "../x", "réd"])
    def test_bad_slugs_are_refused(self, bad, capsys) -> None:
        with pytest.raises(SystemExit) as exc:
            pdd.build_parser().parse_args([f"--project-slug={bad}"])
        assert exc.value.code == 2
        assert "must match" in capsys.readouterr().err

    def test_good_slug(self) -> None:
        assert pdd.build_parser().parse_args(["--project-slug=red-star-2"]).project_slug == "red-star-2"
        assert pdd.build_parser().parse_args([f"--project-slug={'a' * 64}"]).project_slug == "a" * 64


# ---------------------------------------------------------------------------
# Project lookup and RLS
# ---------------------------------------------------------------------------


class TestFindProject:
    def test_unscoped_hit_then_binds_the_projects_workspace(self) -> None:
        conn = FakeConn(projects_by_scope={None: _project_row()})
        project, tried = _run(pdd.find_project(conn, "red-star"))
        assert project is not None and project.project_id == _PID and project.workspace_id == _WS_B
        assert tried == ["unscoped"]
        assert conn.scope == _WS_B  # every later query runs inside the project's workspace

    def test_rls_hiding_the_row_falls_back_to_walking_workspaces(self) -> None:
        conn = FakeConn(projects_by_scope={None: None, _WS_A: None, _WS_B: _project_row()}, workspaces=(_WS_A, _WS_B))
        project, tried = _run(pdd.find_project(conn, "red-star"))
        assert project is not None and project.workspace_id == _WS_B
        assert tried == ["unscoped", f"workspace {_WS_A}", f"workspace {_WS_B}"]
        assert conn.scope == _WS_B

    def test_not_found_leaves_the_session_unscoped(self) -> None:
        conn = FakeConn(projects_by_scope={}, workspaces=(_WS_A, _WS_B))
        project, tried = _run(pdd.find_project(conn, "nope"))
        assert project is None
        assert tried == ["unscoped", f"workspace {_WS_A}", f"workspace {_WS_B}"]
        assert conn.scope is None

    def test_a_project_with_no_workspace_is_read_unscoped(self) -> None:
        conn = FakeConn(projects_by_scope={None: _project_row(ws=None)})
        project, _ = _run(pdd.find_project(conn, "red-star"))
        assert project is not None and project.workspace_id is None
        assert conn.scope is None

    def test_details_select_only_the_columns_that_exist(self) -> None:
        conn = FakeConn(
            rules=[
                ("information_schema.columns", [{"column_name": "project_name"}, {"column_name": "crs_epsg"}]),
                ("FROM silver.projects p WHERE p.project_id", {"project_name": "Red Star", "crs_epsg": 32613}),
            ]
        )
        details = _run(pdd.project_details(conn, _project()))
        assert details["project_name"] == "Red Star" and details["crs_epsg"] == 32613
        assert "commodity" in details["columns_missing"] and "lifecycle_state" in details["columns_missing"]
        assert 'p."project_name", p."crs_epsg"' in conn.sql[-1]


# ---------------------------------------------------------------------------
# The guard every check runs under
# ---------------------------------------------------------------------------


class TestGuarded:
    def test_table_absent_is_reported_not_raised(self) -> None:
        conn = FakeConn(tables={"silver.collars"})
        called = []

        async def body():
            called.append(1)
            return {}

        res = _run(pdd.guarded(conn, ["silver.collars", "silver.nope"], body, what="t"))
        assert res == {"status": "table_absent", "missing_tables": ["silver.nope"]}
        assert not called

    def test_an_exception_is_captured(self) -> None:
        async def body():
            raise RuntimeError("column x does not exist")

        res = _run(pdd.guarded(FakeConn(), [], body, what="t"))
        assert res["status"] == "error" and "RuntimeError: column x does not exist" in res["error"]

    def test_ok_merges_the_data(self) -> None:
        async def body():
            return {"rows": 3}

        assert _run(pdd.guarded(FakeConn(), [], body, what="t")) == {"status": "ok", "rows": 3}


# ---------------------------------------------------------------------------
# Check 1: ingest_progress
# ---------------------------------------------------------------------------


def _progress_rules() -> list[tuple[Any, Any]]:
    rows = [
        {
            "filename": "delivery.zip",
            "status": "partial",
            "current_step": "completed",
            "rows_written": 0,
            "attempt_number": 1,
            "started_at": "2026-09-01 10:00:00+00",
            "completed_at": "2026-09-01 10:05:00+00",
            "failed_at": None,
            "error_text": None,
            "warnings": json.dumps(
                [{"code": "orphaned_intervals", "detail": "SECRET DETAIL"}, {"code": "las_no_depth"}, {"code": "x y z"}]
            ),
        },
        {
            "filename": "b.las",
            "status": "failed",
            "current_step": "failed",
            "rows_written": None,
            "attempt_number": 2,
            "started_at": "2026-09-01 09:00:00+00",
            "completed_at": None,
            "failed_at": "2026-09-01 09:01:00+00",
            "error_text": "E" * 500,
            "warnings": "[]",
        },
    ]
    return [
        ("ip.filename", rows),
        ("GROUP BY ip.status", [{"status": "failed", "n": 3}, {"status": "partial", "n": 1}]),
        (
            "jsonb_array_elements",
            [
                {"code": "orphaned_intervals", "n": 4},
                {"code": "las_no_depth", "n": 2},
                {"code": None, "n": 1},
                {"code": "free text with spaces", "n": 1},
            ],
        ),
    ]


class TestIngestProgress:
    def test_rollups_codes_and_truncation(self) -> None:
        conn = FakeConn(rules=_progress_rules())
        out = _run(pdd.check_ingest_progress(conn, _project()))
        assert out["rows_shown"] == 2 and out["runs_total"] == 4
        assert out["by_status"] == {"failed": 3, "partial": 1}
        assert out["runs"][0]["warning_codes"] == ["orphaned_intervals", "las_no_depth", "(non-code text)"]
        assert out["runs"][1]["error_text"].endswith("...[truncated]")
        assert out["by_warning_code"] == {
            "orphaned_intervals": 4,
            "las_no_depth": 2,
            "(no code)": 1,
            "(non-code text)": 1,
        }
        assert out["watched_codes_seen"] == {"las_no_depth": 2, "orphaned_intervals": 4}
        assert (
            "rows_rejected" in out["watched_codes_not_seen"]
            and "orphaned_intervals" not in out["watched_codes_not_seen"]
        )
        assert out["runs_with_zero_rows_written"] == 1

    def test_warning_detail_text_is_never_in_the_result(self) -> None:
        conn = FakeConn(rules=_progress_rules())
        out = _run(pdd.check_ingest_progress(conn, _project()))
        assert "SECRET DETAIL" not in json.dumps(out)

    def test_the_query_is_scoped_to_the_project_and_limited(self) -> None:
        conn = FakeConn(rules=_progress_rules())
        _run(pdd.check_ingest_progress(conn, _project()))
        first = _norm(conn.sql[0])
        assert (
            "WHERE ip.project_id = $1::uuid" in first
            and "LIMIT 300" in first
            and "ORDER BY ip.started_at DESC" in first
        )
        assert all(a == (_PID,) for a in conn.args)

    def test_table_absent_via_the_runner(self) -> None:
        conn = FakeConn(tables=ALL_TABLES - {"silver.ingest_progress"})
        res = _run(pdd.run_checks(conn, _project()))["ingest_progress"]
        assert res["status"] == "table_absent" and res["missing_tables"] == ["silver.ingest_progress"]


# ---------------------------------------------------------------------------
# Check 2: row counts
# ---------------------------------------------------------------------------


class TestRowCounts:
    COUNTS = {
        "silver.collars": 12,
        "silver.surveys": 0,
        "silver.lithology_logs": 340,
        "silver.well_log_curves": 9,
    }

    def _conn(self, **kw) -> FakeConn:
        def _count(sql, args):
            m = re.search(r"FROM (\S+) t\b", _norm(sql))
            assert m is not None
            return self.COUNTS.get(m.group(1), 0)

        return FakeConn(rules=[("SELECT count(*) FROM", _count)], **kw)

    def test_every_requested_table_is_counted(self) -> None:
        out = _run(pdd.check_row_counts(self._conn(), _project()))
        assert set(out["tables"]) == {
            "silver.collars",
            "silver.surveys",
            "silver.lithology_logs",
            "silver.lithology",
            "silver.structure",
            "silver.well_log_curves",
            "silver.samples",
            "silver.drill_traces",
            "gold.drillhole_intervals_visual",
            "gold.structure_measurements_visual",
            "silver.attribute_tables",
            "silver.spatial_features",
        }
        assert out["tables"]["silver.collars"] == {"status": "ok", "rows": 12}
        assert out["tables"]["silver.surveys"] == {"status": "ok", "rows": 0}
        assert out["tables"]["silver.lithology_logs"]["rows"] == 340

    def test_collar_keyed_tables_scope_through_the_collars_join(self) -> None:
        conn = self._conn()
        _run(pdd.check_row_counts(conn, _project()))
        counts = [_norm(s) for s in conn.sql if "SELECT count(*) FROM" in s]
        by_table = {re.search(r"FROM (\S+) t\b", s).group(1): s for s in counts}  # type: ignore[union-attr]
        for table in (
            "silver.surveys",
            "silver.lithology_logs",
            "silver.lithology",
            "silver.structure",
            "silver.samples",
            "silver.well_log_curves",
        ):
            assert "JOIN silver.collars c ON c.collar_id = t.collar_id WHERE c.project_id = $1::uuid" in by_table[table]
        for table in (
            "silver.collars",
            "silver.drill_traces",
            "gold.drillhole_intervals_visual",
            "silver.attribute_tables",
            "silver.spatial_features",
        ):
            assert by_table[table].endswith("WHERE t.project_id = $1::uuid")

    def test_an_absent_table_is_named_and_the_rest_still_count(self) -> None:
        conn = self._conn(tables=ALL_TABLES - {"silver.structure", "gold.structure_measurements_visual"})
        out = _run(pdd.check_row_counts(conn, _project()))["tables"]
        assert out["silver.structure"] == {"status": "table_absent", "missing_tables": ["silver.structure"]}
        assert out["gold.structure_measurements_visual"]["status"] == "table_absent"
        assert out["silver.collars"]["rows"] == 12

    def test_one_failing_count_does_not_lose_the_others(self) -> None:
        conn = FakeConn(
            rules=[
                (lambda s: "FROM silver.samples t" in s, RuntimeError("permission denied")),
                ("SELECT count(*) FROM", 5),
            ]
        )
        out = _run(pdd.check_row_counts(conn, _project()))["tables"]
        assert out["silver.samples"]["status"] == "error" and "permission denied" in out["silver.samples"]["error"]
        assert out["silver.collars"]["rows"] == 5


# ---------------------------------------------------------------------------
# Check 3: attribute tables — keys, never values
# ---------------------------------------------------------------------------


class TestAttributeTables:
    def _conn(self) -> FakeConn:
        groups = [
            {
                "source_file": "legend.dbf",
                "source_file_sha256": "a" * 64,
                "source_layer": "legend",
                "row_count": 40,
                "first_row_index": 0,
                "total_groups": 2,
            },
            {
                "source_file": "points.dbf",
                "source_file_sha256": "b" * 64,
                "source_layer": "points",
                "row_count": 7,
                "first_row_index": 3,
                "total_groups": 2,
            },
        ]
        keys = {"legend": ["CODE", "DESC"], "points": ["ID", "X", "Y"]}
        return FakeConn(
            rules=[
                ("total_groups", groups),
                ("jsonb_object_keys", lambda sql, args: [{"key": k} for k in keys[args[2]]]),
            ]
        )

    def test_row_counts_and_column_names_per_group(self) -> None:
        out = _run(pdd.check_attribute_tables(self._conn(), _project()))
        assert out["groups_total"] == 2
        assert out["groups"][0] == {
            "source_file": "legend.dbf",
            "source_layer": "legend",
            "sha256_prefix": "a" * 12,
            "row_count": 40,
            "column_names": ["CODE", "DESC"],
        }
        assert out["groups"][1]["column_names"] == ["ID", "X", "Y"]

    def test_the_key_query_reads_the_lowest_row_and_never_selects_values(self) -> None:
        conn = self._conn()
        _run(pdd.check_attribute_tables(conn, _project()))
        key_queries = [_norm(s) for s in conn.sql if "jsonb_object_keys" in s]
        assert len(key_queries) == 2
        for q in key_queries:
            assert q.startswith("SELECT k AS key FROM silver.attribute_tables a")
            assert "a.row_index = $4::int" in q
        # The only place `attributes` may appear in ANY statement is as the argument of jsonb_object_keys.
        for s in conn.sql:
            assert "attributes" not in _norm(s).replace("jsonb_object_keys(a.attributes)", "")
        # the sample row is the group's first row_index
        assert conn.args[-1][3] == 3

    def test_rendered_report_has_names_but_no_values(self) -> None:
        conn = self._conn()
        res = _run(pdd.run_checks(conn, _project()))["attribute_tables"]
        md = "\n".join(pdd._RENDERERS["attribute_tables"](res))
        assert "CODE, DESC" in md and "legend.dbf" in md


# ---------------------------------------------------------------------------
# Check 4: collars
# ---------------------------------------------------------------------------


def _collar_rules(**over: Any) -> list[tuple[Any, Any]]:
    rules: dict[str, Any] = {
        "count(*) FILTER (WHERE c.geom_4326 IS NULL)": {"total": 10, "null_geom": 3, "null_hole_id_canonical": 2},
        "COALESCE(c.georef_method": [
            {"georef_method": "declared", "n": 6},
            {"georef_method": "(null)", "n": 3},
            {"georef_method": "assumed", "n": 1},
        ],
        "GROUP BY c.hole_id_canonical": [{"hole_id": "RS001", "n": 2, "total": 1}],
        "regexp_replace(c.hole_id": [{"hole_id": "RS001", "n": 2, "total": 2}, {"hole_id": "RS9", "n": 2, "total": 2}],
        "ST_Intersects": [{"hole_id": "RS-004", "total": 2}, {"hole_id": "RS-005", "total": 2}],
    }
    rules.update(over)
    return list(rules.items())


class TestCollars:
    def test_the_full_picture(self) -> None:
        out = _run(pdd.check_collars(FakeConn(rules=_collar_rules()), _project()))
        assert (out["total"], out["null_geometry"], out["null_hole_id_canonical"]) == (10, 3, 2)
        assert out["georef_method"] == {"status": "ok", "breakdown": {"declared": 6, "(null)": 3, "assumed": 1}}
        dup = out["duplicates"]
        assert dup["by_hole_id_canonical"] == {"groups": 1, "hole_ids": [{"hole_id": "RS001", "rows": 2}]}
        assert dup["by_normalised_hole_id"]["groups"] == 2
        wy = out["wyoming_fallback_box"]
        assert (
            wy["count"] == 2
            and wy["hole_ids"] == ["RS-004", "RS-005"]
            and wy["box_lng_lat"] == [-111.0, 41.0, -104.0, 45.0]
        )

    def test_no_duplicates_and_nothing_in_wyoming(self) -> None:
        conn = FakeConn(
            rules=_collar_rules(
                **{"GROUP BY c.hole_id_canonical": [], "regexp_replace(c.hole_id": [], "ST_Intersects": []}
            )
        )
        out = _run(pdd.check_collars(conn, _project()))
        assert out["duplicates"]["by_hole_id_canonical"] == {"groups": 0, "hole_ids": []}
        assert out["wyoming_fallback_box"]["count"] == 0

    def test_a_missing_column_fails_only_its_own_sub_check(self) -> None:
        conn = FakeConn(rules=_collar_rules(ST_Intersects=RuntimeError("column c.geom_4326 does not exist")))
        out = _run(pdd.check_collars(conn, _project()))
        assert out["wyoming_fallback_box"]["status"] == "error"
        assert "geom_4326" in out["wyoming_fallback_box"]["error"]
        assert out["georef_method"]["status"] == "ok" and out["duplicates"]["status"] == "ok"
        assert out["total"] == 10

    def test_wyoming_sql_uses_the_box_and_is_capped(self) -> None:
        conn = FakeConn(rules=_collar_rules())
        _run(pdd.check_collars(conn, _project()))
        sql = next(_norm(s) for s in conn.sql if "ST_Intersects" in s)
        assert "ST_MakeEnvelope(-111.0, 41.0, -104.0, 45.0, 4326)" in sql and "LIMIT 50" in sql
        assert "c.project_id = $1::uuid" in sql

    def test_via_the_runner_an_absent_collars_table_is_reported(self) -> None:
        conn = FakeConn(tables=ALL_TABLES - {"silver.collars"}, rules=_collar_rules())
        res = _run(pdd.run_checks(conn, _project()))
        assert res["collars"]["status"] == "table_absent"
        assert res["coverage"]["status"] == "table_absent"
        assert res["curves"]["status"] == "table_absent"


# ---------------------------------------------------------------------------
# Check 5: coverage
# ---------------------------------------------------------------------------


def _has(*needles: str, lacks: tuple[str, ...] = ()) -> Callable[[str], bool]:
    return lambda s: all(n in s for n in needles) and not any(n in s for n in lacks)


def _holes(*ids: str) -> list[dict]:
    return [{"hole_id": h, "total": len(ids)} for h in ids]


def _coverage_rules() -> list[tuple[Any, Any]]:
    return [
        ("SELECT count(*) FROM silver.collars c", 12),
        (_has("silver.lithology_logs l", "NOT EXISTS (SELECT 1 FROM silver.surveys"), _holes("H1", "H2")),
        (_has("silver.lithology_logs l", "NOT EXISTS (SELECT 1 FROM silver.drill_traces"), _holes("H1")),
        (_has("EXISTS (SELECT 1 FROM silver.well_log_curves", lacks=("NOT EXISTS",)), _holes("H3")),
        (
            _has(
                "EXISTS (SELECT 1 FROM silver.surveys s",
                "NOT EXISTS (SELECT 1 FROM silver.drill_traces",
                lacks=("silver.lithology_logs",),
            ),
            _holes("H4"),
        ),
        (
            _has("NOT EXISTS (SELECT 1 FROM silver.drill_traces", lacks=("silver.lithology_logs", "silver.surveys")),
            _holes("H1", "H5", "H6"),
        ),
        (
            "array_agg(c.hole_id",
            [
                {"trace_quality": "ok", "n": 5, "hole_ids": ["A", "B"]},
                {"trace_quality": "single_survey_vertical", "n": 2, "hole_ids": ["C"]},
            ],
        ),
    ]


class TestCoverage:
    def test_lists_counts_and_buckets(self) -> None:
        out = _run(pdd.check_coverage(FakeConn(rules=_coverage_rules()), _project()))
        assert out["holes_total"] == {"status": "ok", "count": 12}
        assert out["lithology_no_surveys"]["hole_ids"] == ["H1", "H2"] and out["lithology_no_surveys"]["count"] == 2
        assert out["with_curves"]["hole_ids"] == ["H3"]
        assert out["surveys_no_trace"]["hole_ids"] == ["H4"]
        assert out["lithology_no_trace"]["hole_ids"] == ["H1"]
        assert out["no_trace"]["hole_ids"] == ["H1", "H5", "H6"] and out["no_trace"]["limit"] == 50
        assert out["trace_quality"]["buckets"]["ok"] == {"count": 5, "hole_ids": ["A", "B"], "limit": 50}
        assert out["trace_quality"]["buckets"]["single_survey_vertical"]["count"] == 2

    def test_lists_are_capped_and_project_scoped_in_sql(self) -> None:
        conn = FakeConn(rules=_coverage_rules())
        _run(pdd.check_coverage(conn, _project()))
        lists = [_norm(s) for s in conn.sql if "count(*) OVER () AS total" in s]
        assert len(lists) == 5
        for q in lists:
            assert "c.project_id = $1::uuid" in q and q.endswith("LIMIT 50")
        assert "[1:50]" in next(_norm(s) for s in conn.sql if "array_agg" in s)

    def test_lithology_counts_the_canonical_table_only_when_it_exists(self) -> None:
        with_canon = FakeConn(rules=_coverage_rules())
        _run(pdd.check_coverage(with_canon, _project()))
        assert any("silver.lithology l2" in s for s in with_canon.sql)
        without = FakeConn(tables=ALL_TABLES - {"silver.lithology"}, rules=_coverage_rules())
        out = _run(pdd.check_coverage(without, _project()))
        assert not any("silver.lithology l2" in s for s in without.sql)
        assert out["lithology_no_surveys"]["status"] == "ok"

    def test_without_the_traces_table_only_the_trace_sub_checks_are_absent(self) -> None:
        conn = FakeConn(tables=ALL_TABLES - {"silver.drill_traces"}, rules=_coverage_rules())
        out = _run(pdd.check_coverage(conn, _project()))
        for key in ("no_trace", "surveys_no_trace", "lithology_no_trace", "trace_quality"):
            assert out[key] == {"status": "table_absent", "missing_tables": ["silver.drill_traces"]}, key
        assert out["lithology_no_surveys"]["status"] == "ok" and out["with_curves"]["status"] == "ok"

    def test_render_shows_the_ids(self) -> None:
        out = _run(pdd.check_coverage(FakeConn(rules=_coverage_rules()), _project()))
        md = "\n".join(pdd._render_coverage(out))
        assert "`H1`, `H2`" in md and "single_survey_vertical" in md


# ---------------------------------------------------------------------------
# Checks 6-8
# ---------------------------------------------------------------------------


class TestCurves:
    def test_counts_by_curve_name(self) -> None:
        rows = [
            {"curve_name": "GAMMA", "curves": 9, "holes": 9, "total_names": 2},
            {"curve_name": "RES", "curves": 3, "holes": 3, "total_names": 2},
        ]
        out = _run(pdd.check_curves(FakeConn(rules=[("count(DISTINCT w.collar_id)", rows)]), _project()))
        assert out["curve_names_total"] == 2 and out["curve_rows_total"] == 12
        assert out["curves"][0] == {"curve_name": "GAMMA", "curves": 9, "holes": 9}

    def test_no_curves(self) -> None:
        out = _run(pdd.check_curves(FakeConn(), _project()))
        assert out["curve_names_total"] == 0 and out["curves"] == []


class TestDerived:
    def test_derived_vs_logged_silver_and_gold(self) -> None:
        conn = FakeConn(
            rules=[
                ("l.lithology_code AS code", [{"code": "DERIVED-ORE", "n": 4}, {"code": "DERIVED-HOST", "n": 6}]),
                (
                    "FROM silver.lithology_logs l",
                    [{"derived": True, "intervals": 10, "holes": 3}, {"derived": False, "intervals": 200, "holes": 8}],
                ),
                ("FROM gold.drillhole_intervals_visual g", [{"derived": True, "intervals": 10, "holes": 3}]),
            ]
        )
        out = _run(pdd.check_derived(conn, _project()))
        s = out["silver_lithology_logs"]
        assert s["derived"] == {"intervals": 10, "holes": 3} and s["logged"] == {"intervals": 200, "holes": 8}
        assert s["derived_codes"] == {"DERIVED-ORE": 4, "DERIVED-HOST": 6}
        g = out["gold_drillhole_intervals_visual"]
        assert g["derived"]["intervals"] == 10 and g["logged"] == {"intervals": 0, "holes": 0}
        gold_sql = next(_norm(x) for x in conn.sql if "FROM gold.drillhole_intervals_visual g" in x)
        assert "g.project_id = $1::uuid" in gold_sql and "interval_kind = 'lithology'" in gold_sql

    def test_gold_table_absent_leaves_silver(self) -> None:
        conn = FakeConn(tables=ALL_TABLES - {"gold.drillhole_intervals_visual"})
        out = _run(pdd.check_derived(conn, _project()))
        assert out["gold_drillhole_intervals_visual"]["status"] == "table_absent"
        assert out["silver_lithology_logs"]["status"] == "ok"


class TestArchiveRuns:
    def test_runs(self) -> None:
        rows = [
            {
                "archive_run_id": "r1",
                "filename": "delivery.zip",
                "status": "partial",
                "file_count": 5,
                "files_succeeded": 3,
                "files_failed": 2,
                "files_skipped": 0,
                "started_at": "2026-09-01 10:00:00+00",
                "completed_at": None,
                "failed_at": None,
                "error_text": "x" * 400,
            }
        ]
        out = _run(pdd.check_archive_runs(FakeConn(rules=[("FROM silver.archive_ingest_runs a", rows)]), _project()))
        assert out["by_status"] == {"partial": 1}
        assert out["runs"][0]["files_failed"] == 2 and out["runs"][0]["error_text"].endswith("...[truncated]")

    def test_absent_table_is_reported(self) -> None:
        conn = FakeConn(tables=ALL_TABLES - {"silver.archive_ingest_runs"})
        res = _run(pdd.run_checks(conn, _project()))["archive_runs"]
        assert res["status"] == "table_absent" and res["missing_tables"] == ["silver.archive_ingest_runs"]


# ---------------------------------------------------------------------------
# End to end: markers, JSON, exit codes, read-only
# ---------------------------------------------------------------------------


def _full_conn(**kw) -> FakeConn:
    rules: list[tuple[Any, Any]] = [
        (
            "information_schema.columns",
            [{"column_name": "project_name"}, {"column_name": "commodity"}, {"column_name": "crs_epsg"}],
        ),
        (
            "FROM silver.projects p WHERE p.project_id",
            {"project_name": "Red Star", "commodity": "U", "crs_epsg": 32613},
        ),
        *_progress_rules(),
        *_collar_rules(),
        *_coverage_rules(),
        ("count(DISTINCT w.collar_id)", [{"curve_name": "GAMMA", "curves": 2, "holes": 2, "total_names": 1}]),
        ("total_groups", []),
        ("SELECT count(*) FROM", lambda sql, args: 0 if "silver.surveys t" in sql else 4),
    ]
    return FakeConn(projects_by_scope={None: _project_row()}, rules=rules, **kw)


def _parse(stdout: str) -> tuple[str, dict]:
    md = stdout.split(pdd.BEGIN_SUMMARY, 1)[1].split(pdd.END_SUMMARY, 1)[0]
    js = json.loads(stdout.split(pdd.BEGIN_JSON, 1)[1].split(pdd.END_JSON, 1)[0])
    return md, js


class TestRun:
    ARGS = argparse.Namespace(project_slug="red-star")

    def test_a_run_prints_both_blocks_and_the_json_parses(self, capsys) -> None:
        rc = _run(pdd.run(self.ARGS, _full_conn()))
        out = capsys.readouterr().out
        assert rc == 0
        assert (
            out.index(pdd.BEGIN_SUMMARY)
            < out.index(pdd.END_SUMMARY)
            < out.index(pdd.BEGIN_JSON)
            < out.index(pdd.END_JSON)
        )
        md, js = _parse(out)
        assert js["project"]["slug"] == "red-star" and js["project"]["project_name"] == "Red Star"
        assert set(js["checks"]) == {
            "ingest_progress",
            "row_counts",
            "attribute_tables",
            "collars",
            "coverage",
            "curves",
            "derived",
            "archive_runs",
        }
        assert js["meta"]["checks_errored"] == 0
        for heading in (
            "## 0. Project",
            "## 1. Ingest runs",
            "## 2. Row counts",
            "## 3. Attribute tables",
            "## 4. Collars",
            "## 5. Per-hole coverage",
            "## 6. Well-log curves",
            "## 7. Derived",
            "## 8. Archive runs",
        ):
            assert heading in md
        assert "Empty for this project" in md and "`silver.surveys`" in md  # surveys count was scripted as 0

    def test_free_text_stays_out_of_the_report(self, capsys) -> None:
        _run(pdd.run(self.ARGS, _full_conn()))
        out = capsys.readouterr().out
        assert "SECRET DETAIL" not in out
        assert "x y z" not in out  # a free-text 'code' is replaced by a placeholder

    def test_tables_absent_are_reported_and_the_run_still_succeeds(self, capsys) -> None:
        conn = _full_conn(tables={"silver.projects", "silver.collars"})
        rc = _run(pdd.run(self.ARGS, conn))
        _, js = _parse(capsys.readouterr().out)
        assert rc == 0
        assert js["checks"]["archive_runs"]["status"] == "table_absent"
        assert js["checks"]["row_counts"]["tables"]["silver.samples"]["status"] == "table_absent"
        assert js["meta"]["checks_table_absent"] >= 3 and js["meta"]["checks_errored"] == 0

    def test_a_failing_sub_check_is_counted_and_headlined(self, capsys) -> None:
        conn = _full_conn()
        conn.rules.insert(0, ("ST_Intersects", RuntimeError("boom")))
        rc = _run(pdd.run(self.ARGS, conn))
        md, js = _parse(capsys.readouterr().out)
        assert rc == 0
        assert js["meta"]["errored_paths"] == ["collars.wyoming_fallback_box"]
        assert "Checks that ERRORED" in md and "boom" in md

    def test_slug_not_found_is_exit_2_with_a_report(self, capsys) -> None:
        conn = FakeConn(projects_by_scope={}, workspaces=(_WS_A, _WS_B))
        rc = _run(pdd.run(argparse.Namespace(project_slug="nope"), conn))
        cap = capsys.readouterr()
        assert rc == 2
        assert "PROJECT NOT FOUND" in cap.err
        md, js = _parse(cap.out)
        assert "PROJECT NOT FOUND OR NOT VISIBLE" in md
        assert js["project"] is None and js["meta"]["scopes_tried"][0] == "unscoped"

    def test_main_maps_an_unexpected_error_to_exit_1(self, monkeypatch, capsys) -> None:
        async def boom():
            raise RuntimeError("cannot reach the database")

        monkeypatch.setattr(pdd, "open_readonly_connection", boom)
        rc = pdd.main(["--project-slug=red-star"])
        assert rc == 1
        assert "cannot reach the database" in capsys.readouterr().err

    def test_main_maps_not_found_to_exit_2(self, monkeypatch, capsys) -> None:
        async def fake_open():
            return FakeConn(projects_by_scope={})

        monkeypatch.setattr(pdd, "open_readonly_connection", fake_open)
        assert pdd.main(["--project-slug=nope"]) == 2

    def test_main_rejects_a_missing_slug(self) -> None:
        with pytest.raises(SystemExit) as exc:
            pdd.main([])
        assert exc.value.code == 2


class TestReadOnly:
    def test_the_connection_is_made_read_only(self, monkeypatch) -> None:
        import asyncpg

        executed: list[str] = []

        class _Conn:
            async def execute(self, sql, *a):
                executed.append(sql)

        async def fake_connect(dsn, **kw):
            return _Conn()

        monkeypatch.setattr(asyncpg, "connect", fake_connect)
        _run(pdd.open_readonly_connection())
        assert "SET default_transaction_read_only = on" in executed

    def test_every_statement_a_full_run_sends_is_a_select_or_the_scope_bind(self) -> None:
        conn = _full_conn()
        _run(pdd.run(argparse.Namespace(project_slug="red-star"), conn))
        assert len(conn.sql) > 40
        forbidden = {"INSERT", "UPDATE", "DELETE", "TRUNCATE", "DROP", "ALTER", "CREATE", "GRANT", "COPY", "VACUUM"}
        for sql in conn.sql:
            tokens = re.findall(r"[A-Za-z_]+", re.sub(r"'[^']*'", "''", sql).upper())
            assert tokens[0] == "SELECT", sql
            assert not forbidden & set(tokens), sql
        for sql in conn.sql:
            if "set_config" in sql:
                assert "app.workspace_id" in sql

    def test_every_data_query_is_project_scoped(self) -> None:
        conn = _full_conn()
        _run(pdd.run(argparse.Namespace(project_slug="red-star"), conn))
        for sql, args in zip(conn.sql, conn.args, strict=True):
            norm = _norm(sql)
            if any(
                x in norm
                for x in ("set_config", "to_regclass", "information_schema", "silver.workspaces", "WHERE p.slug")
            ):
                continue
            assert "$1" in norm, norm
            assert args and args[0] == _PID, norm
