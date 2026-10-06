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
    "silver.reports",
    "silver.document_passages",
    "silver.ingest_ocr_results",
    "spatial_ref_sys",
    "silver.geochemistry",
    "silver.project_boundaries",
    "silver.geological_formations",
    "silver.historic_workings",
    "silver.seismic_surveys",
    "silver.alteration",
    "silver.mineralization",
    "gold.assay_composites",
    "gold.significant_intersections",
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

    def _projects_matching(self, slug: str) -> list[dict]:
        """What _PROJECT_KEY_SQL returns in the bound scope: exact slug first, then the
        ``{slug}-{8 x [a-z0-9]}`` slugs, sorted."""
        seen = self.projects_by_scope.get(self.scope)
        rows = [] if seen is None else (seen if isinstance(seen, list) else [seen])
        suffixed = re.compile(rf"^{re.escape(slug)}-[a-z0-9]{{8}}$")
        hits = [r for r in rows if r["slug"] == slug or suffixed.match(r["slug"])]
        return sorted(hits, key=lambda r: (r["slug"] != slug, r["slug"]))

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        self.args.append(args)
        return self._answer(sql, args, None)

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        self.args.append(args)
        if "WHERE p.slug = $1" in sql:
            return self._projects_matching(args[0])
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
        project, tried, _ = _run(pdd.find_project(conn, "red-star"))
        assert project is not None and project.project_id == _PID and project.workspace_id == _WS_B
        assert tried == ["unscoped"]
        assert conn.scope == _WS_B  # every later query runs inside the project's workspace

    def test_rls_hiding_the_row_falls_back_to_walking_workspaces(self) -> None:
        conn = FakeConn(projects_by_scope={None: None, _WS_A: None, _WS_B: _project_row()}, workspaces=(_WS_A, _WS_B))
        project, tried, _ = _run(pdd.find_project(conn, "red-star"))
        assert project is not None and project.workspace_id == _WS_B
        assert tried == ["unscoped", f"workspace {_WS_A}", f"workspace {_WS_B}"]
        assert conn.scope == _WS_B

    def test_not_found_leaves_the_session_unscoped(self) -> None:
        conn = FakeConn(projects_by_scope={}, workspaces=(_WS_A, _WS_B))
        project, tried, ambiguous = _run(pdd.find_project(conn, "nope"))
        assert project is None and ambiguous == []
        assert tried == ["unscoped", f"workspace {_WS_A}", f"workspace {_WS_B}"]
        assert conn.scope is None

    def test_the_name_part_finds_the_suffixed_slug_a_new_project_gets(self) -> None:
        # Project::makeSlug: "Red Star" -> red-star-{8 random [a-z0-9]} (LAR-14).
        conn = FakeConn(
            projects_by_scope={None: None, _WS_B: _project_row(slug="red-star-k3j9x0qa")}, workspaces=(_WS_B,)
        )
        project, _, ambiguous = _run(pdd.find_project(conn, "red-star"))
        assert project is not None and project.slug == "red-star-k3j9x0qa" and ambiguous == []
        assert conn.scope == _WS_B

    def test_an_exact_slug_beats_suffixed_ones(self) -> None:
        rows = [_project_row(slug="red-star-k3j9x0qa"), _project_row(slug="red-star")]
        conn = FakeConn(projects_by_scope={None: rows})
        project, _, _ = _run(pdd.find_project(conn, "red-star"))
        assert project is not None and project.slug == "red-star"

    def test_two_suffixed_matches_pick_neither_and_name_both(self) -> None:
        rows = [_project_row(slug="red-star-zzzzzzzz"), _project_row(slug="red-star-aaaaaaaa")]
        conn = FakeConn(projects_by_scope={None: rows}, workspaces=(_WS_A,))
        project, tried, ambiguous = _run(pdd.find_project(conn, "red-star"))
        assert project is None
        assert ambiguous == ["red-star-aaaaaaaa", "red-star-zzzzzzzz"]
        assert tried == ["unscoped"]  # the first scope that sees candidates decides
        assert conn.scope is None

    def test_a_longer_name_sharing_the_prefix_is_not_a_match(self) -> None:
        rows = [_project_row(slug="red-star-north-k3j9x0qa"), _project_row(slug="red-star-k3j9x0q")]
        conn = FakeConn(projects_by_scope={None: rows})
        project, _, ambiguous = _run(pdd.find_project(conn, "red-star"))
        assert project is None and ambiguous == []

    def test_a_project_with_no_workspace_is_read_unscoped(self) -> None:
        conn = FakeConn(projects_by_scope={None: _project_row(ws=None)})
        project, _, _ = _run(pdd.find_project(conn, "red-star"))
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


def _document_row(**kw) -> dict:
    row = {
        "report_id": "d1",
        "source_object_key": "bronze/ws/proj/NI43-101_Red_Star.pdf",
        "page_count": 120,
        "is_scanned": True,
        "parser_used": "pdf_report",
        "parse_quality_pct": 91.5,
        "text_page_coverage_pct": 12.0,
        "created_at": "2026-09-01 10:00:00+00",
        "passages": 340,
        "image_passages": 120,
        "embedded": 340,
        "unembedded": 0,
        "cohere_parse_passages": 200,
        "tesseract_passages": 0,
        "low_confidence": 3,
        "total_reports": 1,
    }
    row.update(kw)
    return row


def _document_rules(rows: list[dict] | None = None, rollup: list[dict] | None = None) -> list[tuple[Any, Any]]:
    rows = [_document_row()] if rows is None else rows
    for r in rows:
        r["total_reports"] = len(rows)
    if rollup is None:
        rollup = [
            {"modality": "text", "chunk_kind": "narrative", "ocr_method": "cohere_parse", "n": 200, "embedded": 200},
            {"modality": "image", "chunk_kind": "page_image", "ocr_method": "(none)", "n": 120, "embedded": 120},
            {"modality": "text", "chunk_kind": "narrative", "ocr_method": "fitz_native", "n": 20, "embedded": 20},
        ]
    return [
        ("FROM silver.reports r LEFT JOIN silver.document_passages p", rows),
        ("FROM silver.document_passages p JOIN silver.reports r", rollup),
        ("FROM silver.ingest_ocr_results o", {"n": 0, "reports": 0}),
    ]


class TestDocuments:
    def test_rollups_file_names_and_no_titles(self) -> None:
        out = _run(pdd.check_documents(FakeConn(rules=_document_rules()), _project()))
        assert out["reports_total"] == 1 and out["scanned_reports"] == 1
        assert out["reports"][0]["source_file"] == "NI43-101_Red_Star.pdf"  # basename, never the parsed title
        assert out["passages_total"] == 340 and out["embedded_total"] == 340 and out["unembedded_total"] == 0
        assert out["image_passages_total"] == 120
        assert out["by_ocr_method"] == {"(none)": 120, "cohere_parse": 200, "fitz_native": 20}
        assert out["reports_without_passages"] == [] and out["scanned_reports_without_cohere_parse"] == []
        assert out["legacy_ocr_results"] == {"status": "ok", "rows": 0, "reports": 0}

    def test_gaps_are_named_in_the_headlines(self) -> None:
        rows = [
            _document_row(report_id="d1", source_object_key="a/scan.pdf", cohere_parse_passages=0, tesseract_passages=9),
            _document_row(
                report_id="d2", source_object_key="a/empty.pdf", is_scanned=False, passages=0, image_passages=0,
                embedded=0, unembedded=0, cohere_parse_passages=0,
            ),
        ]
        rollup = [{"modality": "text", "chunk_kind": "narrative", "ocr_method": "tesseract", "n": 9, "embedded": 4}]
        conn = FakeConn(rules=_document_rules(rows, rollup))
        checks = _run(pdd.run_checks(conn, _project(), ["documents"]))
        docs = checks["documents"]
        assert docs["reports_without_passages"] == ["empty.pdf"]
        assert docs["scanned_reports_without_cohere_parse"] == ["scan.pdf"]
        assert docs["unembedded_total"] == 5
        heads = "\n".join(pdd.headlines(checks))
        assert "5 of 9 passage(s) have no embedding" in heads
        assert "1 report(s) have ZERO passages: `empty.pdf`" in heads
        assert "1 scanned report(s) have no cohere_parse passage" in heads and "`scan.pdf`" in heads

    def test_no_reports_is_a_headline(self) -> None:
        conn = FakeConn(rules=_document_rules([], []))
        checks = _run(pdd.run_checks(conn, _project(), ["documents"]))
        assert set(checks) == {"documents"}
        assert "nothing was ingested as a document" in "\n".join(pdd.headlines(checks))

    def test_legacy_ocr_table_absent_does_not_sink_the_check(self) -> None:
        conn = FakeConn(tables=ALL_TABLES - {"silver.ingest_ocr_results"}, rules=_document_rules())
        out = _run(pdd.check_documents(conn, _project()))
        assert out["legacy_ocr_results"]["status"] == "table_absent" and out["passages_total"] == 340

    def test_passages_table_absent_is_reported(self) -> None:
        conn = FakeConn(tables=ALL_TABLES - {"silver.document_passages"})
        res = _run(pdd.run_checks(conn, _project()))["documents"]
        assert res["status"] == "table_absent" and res["missing_tables"] == ["silver.document_passages"]

    def test_render(self) -> None:
        out = _run(pdd.check_documents(FakeConn(rules=_document_rules()), _project()))
        md = "\n".join(pdd._render_documents({"status": "ok", **out}))
        assert "1 report(s) (1 shown, limit 200), 1 scanned. 340 passage(s): 340 embedded, 0 NOT embedded" in md
        assert "| NI43-101_Red_Star.pdf | 120 | True |" in md
        assert "| image | page_image | (none) | 120 | 120 |" in md


# ---------------------------------------------------------------------------
# Checks 10 and 11: placement and visibility
# ---------------------------------------------------------------------------

_COLLAR_CENTROID = [-104.0, 58.0]


def _extent_row(**kw: Any) -> dict:
    row: dict[str, Any] = {
        "n_rows": 12,
        "null_geom": 0,
        "outside_wgs84": 0,
        "min_lng": -104.2,
        "min_lat": 57.9,
        "max_lng": -103.8,
        "max_lat": 58.1,
        "centroid_lng": _COLLAR_CENTROID[0],
        "centroid_lat": _COLLAR_CENTROID[1],
        "srids": [4326],
    }
    row.update(kw)
    return row


def _on_table(table: str) -> Callable[[str], bool]:
    return lambda s: f"FROM {table} t WHERE t.project_id = $1::uuid" in s and "AS srids" in s


def _placement_rules(**over: Any) -> list[tuple[Any, Any]]:
    rules: dict[str, tuple[Any, Any]] = {
        "null_en": ("c.easting IS NULL OR c.northing IS NULL", _holes("N1")),
        "degree": ("abs(c.easting) <= 180", _holes("D1", "D2")),
        "null_elev": ("c.elevation_dem_m IS NULL", _holes("E1", "E2", "E3")),
        "terrain_elev": ("c.elevation_dem_m IS NOT NULL", _holes("G1")),
        "orientation": ("c.azimuth IS NULL OR c.dip IS NULL", _holes("O1")),
        "crs": ("FROM silver.projects pr", {"crs_epsg": 32613, "metre_unit": True, "projected": True}),
        "offset_list": (
            _has("ST_MakePoint", "ORDER BY d.dist_m DESC"),
            [{"hole_id": "FAR1", "dist_m": 812.34567}, {"hole_id": "FAR2", "dist_m": 40.0}],
        ),
        "offset_agg": ("count(*) AS compared", {"compared": 10, "over_threshold": 2, "max_m": 812.34567}),
        "trace_list": (_has("ST_StartPoint", "ORDER BY d.dist_m DESC"), [{"hole_id": "T1", "dist_m": 9.5}]),
        "trace_agg": (
            "count(*) AS traces",
            {"traces": 8, "no_collar_position": 1, "over_threshold": 1, "max_m": 9.5},
        ),
        "ext_collars": (_on_table("silver.collars"), _extent_row()),
        "ext_traces": (_on_table("silver.drill_traces"), _extent_row(n_rows=8)),
        "ext_features": (
            _on_table("silver.spatial_features"),
            _extent_row(n_rows=40, null_geom=2, outside_wgs84=1, centroid_lat=59.0, srids=[4326, 0]),
        ),
        "ext_geochem": (_on_table("silver.geochemistry"), _extent_row(n_rows=5, centroid_lat=58.5)),
        "ext_seismic": (_on_table("silver.seismic_surveys"), _extent_row(n_rows=0, min_lng=None, centroid_lng=None)),
        "stations": (
            "count(*) AS stations",
            {
                "stations": 300,
                "holes": 6,
                "null_azimuth": 4,
                "null_dip": 3,
                "dropped_by_desurvey": 6,
                "dip_out_of_range": 2,
                "azimuth_out_of_range": 1,
                "up_hole_stations": 20,
                "up_hole_holes": 2,
            },
        ),
        "over_cap": ("HAVING count(*) > 100", [{"hole_id": "DEEP", "n": 140, "total": 1}]),
        "mixed": (
            "HAVING count(DISTINCT COALESCE(s.source_file",
            [{"hole_id": "MIX1", "n": 2, "total": 2}, {"hole_id": "MIX2", "n": 3, "total": 2}],
        ),
        "positive": ("HAVING min(s.dip) > 0", [{"hole_id": "POS1", "n": 12, "total": 1}]),
    }
    rules.update(over)
    return list(rules.values())


def _visibility_rules(**over: Any) -> list[tuple[Any, Any]]:
    def _silver_count(sql: str, args: tuple) -> dict:
        table = re.search(r"FROM (\S+) t JOIN", _norm(sql)).group(1)  # type: ignore[union-attr]
        return {
            "silver.lithology_logs": {"n_rows": 500, "holes": 20},
            "silver.lithology": {"n_rows": 480, "holes": 19},
            "silver.alteration": {"n_rows": 30, "holes": 5},
        }.get(table, {"n_rows": 0, "holes": 0})

    def _dropped(sql: str, args: tuple) -> dict:
        base = {"n_rows": 500, "null_depth": 0, "to_not_after_from": 0, "negative_from": 0, "to_depth_overflow": 0}
        if "silver.lithology_logs t" in _norm(sql):
            return {**base, "null_depth": 3, "to_not_after_from": 4, "negative_from": 1, "dropped_any": 7}
        return {**base, "n_rows": 480, "dropped_any": 0}

    rules: dict[str, tuple[Any, Any]] = {
        "cap": ("SELECT count(*) AS n FROM silver.collars c WHERE", 1250),
        "first": (
            "AS project_holes_with_bands",
            {"holes_considered": 200, "with_bands": 0, "project_holes_with_bands": 14},
        ),
        "over_80": (
            "HAVING count(*) FILTER (WHERE g.interval_kind = 'lithology') > 80",
            [{"hole_id": "BANDY", "n": 95, "total": 1}],
        ),
        "over_1500": (
            "HAVING count(*) > 1500",
            [{"hole_id": "HUGE", "kind": "alteration", "n": 1600, "total": 1}],
        ),
        "kinds": (
            "GROUP BY g.interval_kind ORDER BY n_rows",
            [{"kind": "lithology", "n_rows": 400, "holes": 14}, {"kind": "sample_window", "n_rows": 90, "holes": 8}],
        ),
        "silver_counts": ("count(DISTINCT t.collar_id) AS holes", _silver_count),
        "logs_no_gold": ("FROM silver.lithology_logs lg", _holes("NOGOLD1", "NOGOLD2")),
        "canon_no_gold": ("FROM silver.lithology lc", []),
        "dropped": ("AS dropped_any", _dropped),
        "structure": (
            "AS unusable_in_3d",
            {"n_rows": 6000, "null_true_dip": 10, "null_true_dip_dir": 12, "unusable_in_3d": 15, "null_depth": 0},
        ),
        "structure_visual": ("AS null_depth FROM gold.structure_measurements_visual t", {"n_rows": 40, "null_depth": 0}),
        "samples": ("AS empty_object", {"n_rows": 7000, "null_assays": 100, "empty_object": 900, "non_empty": 6000}),
        "composites": ("FROM gold.assay_composites t", {"n_rows": 0}),
        "intersections": ("FROM gold.significant_intersections t", {"n_rows": 12}),
        "picker": (
            _has("NOT EXISTS (SELECT 1 FROM silver.well_log_curves wc", "count(*) OVER () AS total"),
            _holes("LOST1", "LOST2", "LOST3"),
        ),
        "picker_hidden": ("r.rn > 1000", 4),
    }
    rules.update(over)
    return list(rules.values())


class TestHaversine:
    def test_known_distances(self) -> None:
        assert pdd.haversine_km(-104.0, 58.0, -104.0, 58.0) == 0.0
        assert pdd.haversine_km(-104.0, 58.0, -104.0, 59.0) == pytest.approx(111.2, abs=0.3)
        assert pdd.haversine_km(0.0, 0.0, 180.0, 0.0) == pytest.approx(20015.1, abs=1.0)


class TestPlacement:
    def test_the_full_picture(self) -> None:
        out = _run(pdd.check_placement(FakeConn(rules=_placement_rules()), _project()))
        assert out["null_easting_or_northing"] == {"status": "ok", "count": 1, "hole_ids": ["N1"], "limit": 50}
        assert out["degree_looking_easting_northing"]["hole_ids"] == ["D1", "D2"]
        assert out["null_elevation"]["count"] == 3
        assert out["terrain_elevation"]["hole_ids"] == ["G1"]
        assert out["no_orientation_and_no_surveys"]["hole_ids"] == ["O1"]
        off = out["easting_northing_vs_geom_4326"]
        assert (off["project_crs_epsg"], off["compared"], off["over_threshold"], off["max_m"]) == (32613, 10, 2, 812.346)
        assert off["holes"][0] == {"hole_id": "FAR1", "distance_m": 812.346}
        tr = out["trace_start_vs_collar"]
        assert (tr["traces"], tr["over_threshold"], tr["max_m"], tr["no_collar_position"]) == (8, 1, 9.5, 1)
        assert tr["holes"] == [{"hole_id": "T1", "distance_m": 9.5}]
        st = out["surveys"]["stations"]
        assert st["dropped_by_desurvey"] == 6 and st["dip_out_of_range"] == 2 and st["up_hole_holes"] == 2
        assert out["surveys"]["over_station_cap"]["holes"] == [{"hole_id": "DEEP", "n": 140}]
        assert out["surveys"]["multiple_source_files"]["count"] == 2
        assert out["surveys"]["all_dips_positive"]["holes"][0]["hole_id"] == "POS1"
        assert "negative = below horizontal" in out["surveys"]["dip_convention"]

    def test_extents_distance_from_the_collar_centroid_and_the_100_km_flag(self) -> None:
        tables = _run(pdd.check_placement(FakeConn(rules=_placement_rules()), _project()))["extents"]["tables"]
        assert tables["collars"]["km_from_collar_centroid"] is None  # the anchor itself
        assert tables["drill_traces"]["km_from_collar_centroid"] == 0.0
        assert tables["drill_traces"]["far_from_collars"] is False
        feats = tables["spatial_features"]
        assert feats["km_from_collar_centroid"] == pytest.approx(111.2, abs=0.3) and feats["far_from_collars"] is True
        assert (feats["rows"], feats["null_or_empty_geometry"], feats["outside_wgs84"]) == (40, 2, 1)
        assert feats["srids"] == [0, 4326]
        assert tables["geochemistry"]["far_from_collars"] is False
        assert tables["geochemistry"]["km_from_collar_centroid"] == pytest.approx(55.6, abs=0.3)
        assert tables["collars"]["bbox_lng_lat"] == [-104.2, 57.9, -103.8, 58.1]
        # an empty table has no bbox and no centroid, and is neither far nor near
        seismic = tables["seismic_surveys"]
        assert seismic["bbox_lng_lat"] is None and seismic["centroid_lng_lat"] is None
        assert seismic["km_from_collar_centroid"] is None and seismic["far_from_collars"] is False

    def test_a_table_that_does_not_exist_is_reported_per_layer(self) -> None:
        conn = FakeConn(tables=ALL_TABLES - {"silver.geochemistry", "silver.historic_workings"}, rules=_placement_rules())
        out = _run(pdd.check_placement(conn, _project()))
        tables = out["extents"]["tables"]
        assert tables["geochemistry"] == {"status": "table_absent", "missing_tables": ["silver.geochemistry"]}
        assert tables["historic_workings"]["status"] == "table_absent"
        assert tables["collars"]["status"] == "ok" and tables["spatial_features"]["status"] == "ok"

    def test_without_surveys_or_traces_only_those_sub_checks_are_absent(self) -> None:
        conn = FakeConn(tables=ALL_TABLES - {"silver.surveys", "silver.drill_traces"}, rules=_placement_rules())
        out = _run(pdd.check_placement(conn, _project()))
        assert out["no_orientation_and_no_surveys"]["status"] == "table_absent"
        assert out["trace_start_vs_collar"] == {"status": "table_absent", "missing_tables": ["silver.drill_traces"]}
        for key in ("stations", "over_station_cap", "multiple_source_files", "all_dips_positive"):
            assert out["surveys"][key]["status"] == "table_absent", key
        assert out["null_elevation"]["status"] == "ok" and out["extents"]["tables"]["collars"]["status"] == "ok"

    def test_a_bad_srid_fails_only_the_offset_sub_check(self) -> None:
        bad = (_has("ST_MakePoint", "count(*) AS compared"), RuntimeError("Invalid SRID: 99999"))
        conn = FakeConn(rules=[bad, *_placement_rules()])
        out = _run(pdd.check_placement(conn, _project()))
        assert out["easting_northing_vs_geom_4326"]["status"] == "error"
        assert "99999" in out["easting_northing_vs_geom_4326"]["error"]
        assert out["null_elevation"]["status"] == "ok" and out["trace_start_vs_collar"]["status"] == "ok"
        assert pdd.errored_paths({"placement": out}) == ["placement.easting_northing_vs_geom_4326"]

    @pytest.mark.parametrize(
        ("crs_row", "why"),
        [
            ({"crs_epsg": None, "metre_unit": None, "projected": None}, "crs_epsg is not set"),
            ({"crs_epsg": 99999, "metre_unit": None, "projected": None}, "not in spatial_ref_sys"),
            ({"crs_epsg": 4326, "metre_unit": False, "projected": False}, "geographic CRS"),
        ],
    )
    def test_the_offset_is_skipped_when_the_project_crs_cannot_carry_metres(self, crs_row, why) -> None:
        conn = FakeConn(rules=_placement_rules(crs=("FROM silver.projects pr", crs_row)))
        off = _run(pdd.check_placement(conn, _project()))["easting_northing_vs_geom_4326"]
        assert off["status"] == "ok" and why in off["skipped"] and "compared" not in off
        assert not any("ST_MakePoint" in s for s in conn.sql)  # no transform was attempted
        assert "skipped" in "\n".join(pdd._render_placement(_run(pdd.check_placement(conn, _project()))))  # noqa: SIM300

    def test_the_sql_is_scoped_capped_and_reads_the_right_columns(self) -> None:
        conn = FakeConn(rules=_placement_rules())
        _run(pdd.check_placement(conn, _project()))
        norm = [_norm(s) for s in conn.sql]
        offset = next(s for s in norm if "count(*) AS compared" in s)
        assert "ST_Transform(c.geom_4326, $2::int)" in offset and "ST_MakePoint(c.easting, c.northing)" in offset
        assert "d.dist_m > 25.0" in offset and "c.project_id = $1::uuid" in offset
        offset_args = conn.args[conn.sql.index(next(s for s in conn.sql if "count(*) AS compared" in s))]
        assert offset_args == (_PID, 32613)
        trace = next(s for s in norm if "count(*) AS traces" in s)
        assert "ST_StartPoint(t.geom)::geography" in trace and "c.geom_4326::geography" in trace
        assert "d.dist_m > 5.0" in trace and "t.project_id = $1::uuid AND c.project_id = $1::uuid" in trace
        for s in norm:
            if "ORDER BY d.dist_m DESC" in s:
                assert s.endswith("LIMIT 50")
        seismic = next(s for s in norm if "FROM silver.seismic_surveys t" in s)
        assert "ST_Transform(t.bbox, 4326)" in seismic and "ST_SRID(t.bbox) IN (0, 4326)" in seismic
        assert "t.project_id = $1::uuid" in seismic
        # collar-keyed survey queries go through silver.collars
        stats = next(s for s in norm if "count(*) AS stations" in s)
        assert "JOIN silver.collars c ON c.collar_id = s.collar_id" in stats and "c.project_id = $1::uuid" in stats

    def test_render_shows_ids_distances_extents_and_the_dip_convention(self) -> None:
        out = _run(pdd.check_placement(FakeConn(rules=_placement_rules()), _project()))
        md = "\n".join(pdd._render_placement(out))
        assert "`D1`, `D2`" in md and "`N1`" in md
        assert "`FAR1` 812.346 m, `FAR2` 40.0 m" in md
        assert "**2** more than 25 m apart, max 812.346 m" in md
        assert "| `spatial_features` | 40 | 2 | 1 | 0,4326 |" in md and "**> 100 km**" in md
        assert "| `seismic_surveys` | 0 | 0 | 0 | 4326 |" in md
        assert "`T1` 9.5 m" in md and "trace_quality buckets are reported under check 5" in md
        assert "dip convention used: negative = below horizontal" in md
        assert "`DEEP` x140" in md and "`MIX2` x3" in md and "`POS1` x12" in md

    def test_render_reports_a_failed_or_absent_sub_check_in_place(self) -> None:
        conn = FakeConn(
            tables=ALL_TABLES - {"silver.surveys"},
            rules=_placement_rules(null_elev=("c.elevation_dem_m IS NULL", RuntimeError("column c.elevation does not exist"))),
        )
        md = "\n".join(pdd._render_placement(_run(pdd.check_placement(conn, _project()))))
        assert "**check failed:** `RuntimeError: column c.elevation does not exist`" in md
        assert "**table absent:** `silver.surveys`" in md

    def test_findings_are_facts_only(self) -> None:
        out = _run(pdd.check_placement(FakeConn(rules=_placement_rules()), _project()))
        text = "\n".join(pdd.placement_findings(out))
        assert "1 collar(s) have NULL easting or northing" in text
        assert "degree-looking" in text and "2 collar(s)" in text
        assert "2 of 10 collar(s) have easting/northing more than 25 m from geom_4326 in EPSG:32613 (max 812.346 m)" in text
        assert "`spatial_features` (111.2 km)" in text
        assert "`spatial_features` x1" in text  # outside WGS84
        assert "1 of 8 drill trace(s) start more than 5 m from their collar (max 9.5 m)" in text
        assert "6 survey station(s) have a NULL azimuth or dip" in text and "outside -90..90" in text
        assert "ONLY positive dips" in text and "more than one source file" in text

    def test_nothing_to_report_gives_no_findings(self) -> None:
        out = _run(pdd.check_placement(FakeConn(), _project()))  # every query answers empty / 0 / None
        assert pdd.placement_findings(out) == []
        assert out["easting_northing_vs_geom_4326"]["skipped"] == "silver.projects.crs_epsg is not set"
        assert out["extents"]["tables"]["collars"]["rows"] == 0


class TestVisibility:
    def test_the_full_picture(self) -> None:
        out = _run(pdd.check_visibility(FakeConn(rules=_visibility_rules()), _project()))
        assert out["collar_cap"] == {"status": "ok", "total": 1250, "cap": 1000, "beyond_cap": 250}
        first = out["first_holes_lithology"]
        assert (first["first_n_with_lithology_bands"], first["project_holes_with_lithology_bands"]) == (0, 14)
        assert first["three_d_empty_trap"] is True and first["first_n"] == 200
        assert out["holes_over_3d_band_cap"]["holes"] == [{"hole_id": "BANDY", "n": 95}]
        assert out["holes_over_strip_band_limit"]["holes"] == [{"hole_id": "HUGE", "n": 1600, "kind": "alteration"}]
        assert out["gold_intervals_by_kind"]["kinds"]["lithology"] == {"rows": 400, "holes": 14}
        assert out["silver_logs"]["lithology_logs"] == {"status": "ok", "rows": 500, "holes": 20}
        assert out["silver_logs"]["mineralization"] == {"status": "ok", "rows": 0, "holes": 0}
        assert out["lithology_logs_without_gold_bands"]["hole_ids"] == ["NOGOLD1", "NOGOLD2"]
        assert out["canonical_lithology_without_gold_bands"]["count"] == 0
        drops = out["lithology_rows_promotion_drops"]
        assert drops["lithology_logs"]["dropped_any"] == 7 and drops["lithology"]["dropped_any"] == 0
        assert drops["lithology_logs"]["rows"] == 500 and drops["lithology_logs"]["null_depth"] == 3
        assert out["structure"]["unusable_in_3d"] == 15 and out["structure"]["over_3d_cap"] is True
        assert out["structure_measurements_visual"] == {
            "status": "ok",
            "rows": 40,
            "null_depth": 0,
            "over_3d_cap": False,
            "cap": 5000,
        }
        smp = out["samples"]
        assert (smp["null_assays"], smp["empty_object"], smp["non_empty"], smp["non_null"]) == (100, 900, 6000, 6900)
        assert smp["over_3d_cap"] is True
        assert out["gold_tables_without_writer"]["assay_composites"]["note"] == "no writer in the codebase"
        assert out["gold_tables_without_writer"]["significant_intersections"] == {"status": "ok", "rows": 12, "note": None}
        assert out["absent_from_logs_picker"]["hole_ids"] == ["LOST1", "LOST2", "LOST3"]
        assert out["picker_hidden_by_collar_cap"] == {"status": "ok", "count": 4, "cap": 1000}

    def test_no_trap_when_the_first_200_have_bands_or_nothing_has(self) -> None:
        ok = {"holes_considered": 200, "with_bands": 12, "project_holes_with_bands": 14}
        empty = {"holes_considered": 200, "with_bands": 0, "project_holes_with_bands": 0}
        for row in (ok, empty):
            rules = _visibility_rules(first=("AS project_holes_with_bands", row))
            out = _run(pdd.check_visibility(FakeConn(rules=rules), _project()))
            assert out["first_holes_lithology"]["three_d_empty_trap"] is False

    def test_the_first_200_are_taken_in_the_controllers_order(self) -> None:
        conn = FakeConn(rules=_visibility_rules())
        _run(pdd.check_visibility(conn, _project()))
        sql = next(_norm(s) for s in conn.sql if "AS project_holes_with_bands" in s)
        assert "ORDER BY c.hole_id, c.collar_id LIMIT 200" in sql and "g1.interval_kind = 'lithology'" in sql
        hidden = next(_norm(s) for s in conn.sql if "r.rn > 1000" in s)
        assert "row_number() OVER (ORDER BY c.hole_id, c.collar_id)" in hidden

    def test_the_sql_is_scoped_and_lists_are_capped(self) -> None:
        conn = FakeConn(rules=_visibility_rules())
        _run(pdd.check_visibility(conn, _project()))
        for sql, args in zip(conn.sql, conn.args, strict=True):
            if "to_regclass" in sql:
                continue
            norm = _norm(sql)
            assert "$1::uuid" in norm and args == (_PID,), norm
            if "HAVING" in norm or "count(*) OVER () AS total" in norm:
                assert norm.endswith("LIMIT 50"), norm
            if "gold.drillhole_intervals_visual" in norm or "FROM silver." in norm:
                assert "c.project_id = $1::uuid" in norm or "g.project_id = $1::uuid" in norm or "pr.project_id" in norm, norm
        dropped = next(_norm(s) for s in conn.sql if "FROM silver.lithology t" in s and "AS dropped_any" in s)
        for fragment in ("t.to_depth <= t.from_depth", "t.from_depth < 0", "t.to_depth >= 10000000", "IS NULL"):
            assert fragment in dropped

    def test_gold_tables_without_a_writer_say_so_only_when_empty(self) -> None:
        out = _run(pdd.check_visibility(FakeConn(rules=_visibility_rules()), _project()))
        findings = "\n".join(pdd.visibility_findings(out))
        assert "`gold.assay_composites` is empty for this project (no writer in the codebase)" in findings
        assert "significant_intersections" not in findings

    def test_missing_gold_table_leaves_the_silver_counts(self) -> None:
        conn = FakeConn(tables=ALL_TABLES - {"gold.drillhole_intervals_visual"}, rules=_visibility_rules())
        out = _run(pdd.check_visibility(conn, _project()))
        absent = {"status": "table_absent", "missing_tables": ["gold.drillhole_intervals_visual"]}
        for key in (
            "first_holes_lithology",
            "holes_over_3d_band_cap",
            "holes_over_strip_band_limit",
            "gold_intervals_by_kind",
            "lithology_logs_without_gold_bands",
            "absent_from_logs_picker",
        ):
            assert out[key] == absent, key
        assert out["silver_logs"]["lithology_logs"]["rows"] == 500
        assert out["collar_cap"]["status"] == "ok" and out["samples"]["status"] == "ok"

    def test_a_missing_optional_table_is_absent_not_fatal(self) -> None:
        conn = FakeConn(
            tables=ALL_TABLES - {"silver.alteration", "silver.lithology", "gold.assay_composites"},
            rules=_visibility_rules(),
        )
        out = _run(pdd.check_visibility(conn, _project()))
        assert out["silver_logs"]["alteration"]["status"] == "table_absent"
        assert out["canonical_lithology_without_gold_bands"]["status"] == "table_absent"
        assert out["lithology_rows_promotion_drops"]["lithology"]["status"] == "table_absent"
        assert out["lithology_rows_promotion_drops"]["lithology_logs"]["status"] == "ok"
        assert out["gold_tables_without_writer"]["assay_composites"]["status"] == "table_absent"
        assert out["gold_tables_without_writer"]["significant_intersections"]["status"] == "ok"

    def test_a_failing_query_fails_only_its_own_sub_check(self) -> None:
        rules = _visibility_rules(structure=("AS unusable_in_3d", RuntimeError("column t.true_dip does not exist")))
        out = _run(pdd.check_visibility(FakeConn(rules=rules), _project()))
        assert out["structure"]["status"] == "error" and "true_dip" in out["structure"]["error"]
        assert out["structure_measurements_visual"]["status"] == "ok" and out["samples"]["status"] == "ok"
        assert pdd.errored_paths({"visibility": out}) == ["visibility.structure"]

    def test_render_covers_every_section(self) -> None:
        out = _run(pdd.check_visibility(FakeConn(rules=_visibility_rules()), _project()))
        md = "\n".join(pdd._render_visibility(out))
        assert "collars: **1250**, Workspace cap 1000, beyond the cap: **250**" in md
        assert "**0** have gold lithology bands; project-wide **14** hole(s) do" in md and "will be EMPTY" in md
        assert "`BANDY` x95" in md and "`HUGE` (alteration) x1600" in md
        assert "| lithology | 400 | 14 |" in md and "| `silver.lithology_logs` | 500 | 20 |" in md
        assert "`NOGOLD1`, `NOGOLD2`" in md
        assert "`silver.lithology_logs` rows the promotion drops: **7** of 500 (NULL depth 3, to<=from 4, from<0 1" in md
        assert "6000 row(s); NULL true_dip 10" in md and "over the 3D cap of 5000" in md
        assert "'{}' 900, non-empty 6000" in md
        assert "`gold.assay_composites`: 0 row(s) (no writer in the codebase)" in md
        assert "`LOST1`, `LOST2`, `LOST3`" in md and "not listed): **4**" in md

    def test_render_shows_absent_and_failed_sub_checks_in_place(self) -> None:
        conn = FakeConn(
            tables=ALL_TABLES - {"silver.samples"},
            rules=_visibility_rules(kinds=("GROUP BY g.interval_kind ORDER BY n_rows", RuntimeError("boom"))),
        )
        md = "\n".join(pdd._render_visibility(_run(pdd.check_visibility(conn, _project()))))
        assert "**table absent:** `silver.samples`" in md and "**check failed:** `RuntimeError: boom`" in md

    def test_findings_are_facts_only(self) -> None:
        out = _run(pdd.check_visibility(FakeConn(rules=_visibility_rules()), _project()))
        text = "\n".join(pdd.visibility_findings(out))
        assert "1250 collars: 250 are beyond the Workspace cap of 1000" in text
        assert "3D lithology will be EMPTY: none of the first 200 collars" in text and "14 hole(s)" in text
        assert "1 hole(s) have more than 80 lithology bands" in text
        assert "2 hole(s) have `silver.lithology_logs` rows but no gold lithology band" in text
        assert "7 of 500 `silver.lithology_logs` row(s) are dropped by the promotion" in text
        assert "15 of 6000 `silver.structure` row(s) lack true_dip or true_dip_dir" in text
        assert "`silver.structure` has 6000 rows: 3D loads 5000" in text
        assert "6900 rows with commodity_assays: 3D loads 5000" in text
        assert "3 hole(s) are absent from the LOGS picker" in text
        assert "4 hole(s) with bands but no curves sit beyond the 1000-collar cap" in text

    def test_nothing_to_report_gives_no_free_text_findings(self) -> None:
        out = _run(pdd.check_visibility(FakeConn(), _project()))
        findings = pdd.visibility_findings(out)
        # an empty project: the only facts are the two gold tables with no rows
        assert findings == [
            "`gold.assay_composites` is empty for this project (no writer in the codebase).",
            "`gold.significant_intersections` is empty for this project (no writer in the codebase).",
        ]


class TestPlacementAndVisibilityRun:
    ARGS = argparse.Namespace(project_slug="red-star", only=["placement", "visibility"])

    def test_only_selects_both_new_checks_and_they_render_and_serialise(self, capsys) -> None:
        conn = _full_conn()
        rc = _run(pdd.run(self.ARGS, conn))
        md, js = _parse(capsys.readouterr().out)
        assert rc == 0 and list(js["checks"]) == ["placement", "visibility"]
        assert js["meta"]["checks_errored"] == 0
        assert "## 10. Where the data lands" in md and "## 11. Will the UI show it" in md
        assert js["checks"]["placement"]["extents"]["tables"]["spatial_features"]["far_from_collars"] is True
        assert js["checks"]["visibility"]["first_holes_lithology"]["three_d_empty_trap"] is True
        heads = "\n".join(js["headlines"])
        assert "[placement] " in heads and "[visibility] " in heads and "3D lithology will be EMPTY" in heads

    def test_every_new_statement_is_a_select(self) -> None:
        conn = _full_conn()
        _run(pdd.run(self.ARGS, conn))
        assert any("count(*) AS compared" in s for s in conn.sql) and any("AS project_holes_with_bands" in s for s in conn.sql)

    def test_the_corpus_overview_counts_findings_per_project(self, capsys) -> None:
        conn = _full_conn()
        conn.rules.insert(0, ("FROM silver.projects p ORDER BY p.slug", [_project_row(slug="alpha-aaaaaaaa")]))
        _run(pdd.run(argparse.Namespace(all_projects=True, project_slug=None, only=None), conn))
        md, js = _parse(capsys.readouterr().out)
        assert "| placement findings | visibility findings |" in md
        row = next(line for line in md.splitlines() if line.startswith("| alpha-aaaaaaaa |"))
        cells = [c.strip() for c in row.strip("|").split("|")]
        place = pdd.placement_findings(js["projects"][0]["checks"]["placement"])
        visib = pdd.visibility_findings(js["projects"][0]["checks"]["visibility"])
        assert cells[-2:] == [str(len(place)), str(len(visib))] and len(place) > 0 and len(visib) > 0

    def test_the_overview_shows_a_question_mark_when_the_checks_were_not_run(self, capsys) -> None:
        conn = _full_conn()
        conn.rules.insert(0, ("FROM silver.projects p ORDER BY p.slug", [_project_row(slug="alpha-aaaaaaaa")]))
        _run(pdd.run(argparse.Namespace(all_projects=True, project_slug=None, only=["documents"]), conn))
        md, _ = _parse(capsys.readouterr().out)
        row = next(line for line in md.splitlines() if line.startswith("| alpha-aaaaaaaa |"))
        assert row.rstrip(" |").endswith("| ? | ?")

    def test_the_new_keys_are_accepted_by_only(self) -> None:
        assert pdd._only("placement,visibility") == ["placement", "visibility"]
        assert pdd.CHECK_KEYS[-2:] == ("placement", "visibility")

    def test_a_missing_collars_table_marks_both_checks_absent(self) -> None:
        conn = FakeConn(tables=ALL_TABLES - {"silver.collars"})
        res = _run(pdd.run_checks(conn, _project(), ["placement", "visibility"]))
        for key in ("placement", "visibility"):
            assert res[key] == {
                "status": "table_absent",
                "missing_tables": ["silver.collars"],
                "title": pdd.CHECKS[pdd.CHECK_KEYS.index(key)].title,
            }


class TestOnly:
    def test_only_accepts_known_keys(self) -> None:
        assert pdd._only("documents,row_counts") == ["documents", "row_counts"]

    @pytest.mark.parametrize("bad", ["", "nope", "documents,nope"])
    def test_only_refuses_unknown_keys(self, bad) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            pdd._only(bad)

    def test_run_checks_subset(self) -> None:
        checks = _run(pdd.run_checks(_full_conn(), _project(), ["row_counts", "archive_runs"]))
        assert list(checks) == ["row_counts", "archive_runs"]

    def test_parser_needs_exactly_one_target(self, capsys) -> None:
        with pytest.raises(SystemExit):
            pdd.build_parser().parse_args([])
        with pytest.raises(SystemExit):
            pdd.build_parser().parse_args(["--project-slug=a", "--all-projects"])
        args = pdd.build_parser().parse_args(["--all-projects", "--only=documents"])
        assert args.all_projects is True and args.only == ["documents"] and args.project_slug is None


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
        *_document_rules(),
        *_placement_rules(),
        *_visibility_rules(),
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
            "documents",
            "placement",
            "visibility",
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
            "## 9. Documents, passages and embeddings",
            "## 10. Where the data lands",
            "## 11. Will the UI show it",
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

    def test_an_ambiguous_name_is_exit_2_listing_the_full_slugs(self, capsys) -> None:
        rows = [_project_row(slug="red-star-aaaaaaaa"), _project_row(slug="red-star-bbbbbbbb")]
        conn = FakeConn(projects_by_scope={None: rows})
        rc = _run(pdd.run(argparse.Namespace(project_slug="red-star"), conn))
        cap = capsys.readouterr()
        assert rc == 2
        assert "PROJECT AMBIGUOUS" in cap.err
        md, js = _parse(cap.out)
        assert "MORE THAN ONE PROJECT MATCHES" in md and "`red-star-bbbbbbbb`" in md
        assert js["meta"]["ambiguous_slugs"] == ["red-star-aaaaaaaa", "red-star-bbbbbbbb"]

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


class TestAllProjects:
    ARGS = argparse.Namespace(all_projects=True, project_slug=None, only=None)

    @staticmethod
    def _conn(projects: list[dict]) -> FakeConn:
        conn = _full_conn()
        conn.rules.insert(0, ("FROM silver.projects p ORDER BY p.slug", projects))
        return conn

    def test_every_project_is_reported_behind_an_overview(self, capsys) -> None:
        conn = self._conn([_project_row(slug="alpha-aaaaaaaa"), _project_row(ws=_WS_A, slug="red-star-k3j9x0qa")])
        rc = _run(pdd.run(self.ARGS, conn))
        out = capsys.readouterr().out
        assert rc == 0
        md, js = _parse(out)
        assert js["meta"]["mode"] == "all_projects" and js["meta"]["projects_total"] == 2
        assert [p["project"]["slug"] for p in js["projects"]] == ["alpha-aaaaaaaa", "red-star-k3j9x0qa"]
        assert js["totals"] == {"reports": 2, "passages": 680, "embedded": 680, "unembedded": 0, "image_passages": 240}
        assert "# Corpus diagnostics (all projects)" in md and "## Overview" in md
        assert "| alpha-aaaaaaaa | 1 | 1 | 340 | 340 | 0 | 120 |" in md
        assert "### 9. Documents, passages and embeddings" in md  # per-project sections demoted one level
        # Each project's checks ran inside ITS workspace, and the session is left there
        # only until the next project is bound.
        bound = [a[0] for sql, a in zip(conn.sql, conn.args, strict=True) if "set_config('app.workspace_id'" in sql and a]
        assert _WS_B in bound and _WS_A in bound

    def test_only_limits_the_corpus_run(self, capsys) -> None:
        conn = self._conn([_project_row(slug="alpha-aaaaaaaa")])
        rc = _run(pdd.run(argparse.Namespace(all_projects=True, project_slug=None, only=["documents"]), conn))
        _, js = _parse(capsys.readouterr().out)
        assert rc == 0 and js["meta"]["checks"] == ["documents"]
        assert set(js["projects"][0]["checks"]) == {"documents"}

    def test_no_projects_is_exit_2_with_a_report(self, capsys) -> None:
        conn = self._conn([])
        rc = _run(pdd.run(self.ARGS, conn))
        captured = capsys.readouterr()
        md, js = _parse(captured.out)
        assert rc == 2 and js["projects"] == [] and "NO PROJECTS VISIBLE" in md
        assert "NO PROJECTS VISIBLE" in captured.err

    def test_one_broken_project_does_not_lose_the_others(self, capsys) -> None:
        conn = self._conn([_project_row(slug="alpha-aaaaaaaa"), _project_row(slug="beta-bbbbbbbb")])
        calls = {"n": 0}

        def _explode(sql, args):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return [{"column_name": "project_name"}]

        conn.rules.insert(0, ("information_schema.columns", _explode))
        rc = _run(pdd.run(self.ARGS, conn))
        _, js = _parse(capsys.readouterr().out)
        assert rc == 0 and len(js["projects"]) == 2
        assert js["projects"][0]["checks"]["documents"]["status"] == "error"
        assert js["projects"][1]["checks"]["documents"]["status"] == "ok"
