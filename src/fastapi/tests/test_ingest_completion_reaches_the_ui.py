"""A run that finishes in the database has to finish on the screen too.

Two defects sit behind this file, and both were invisible from inside the
ingestion code — everything logged success.

1.  ingest_tabular, ingest_spatial and ingest_well_logs called
    ``mark_completed_by_run`` and stopped there. Nothing told Laravel, so
    nothing bumped ``silver.projects.data_version``, nothing queued the
    materialised-view refresh, and no Reverb event reached the browser. A
    geologist uploaded a collar CSV, watched Ingestion Runs tick over to
    "Completed", and found the map exactly as they had left it.

2.  'partial' — the status those same workflows produce when rows land and
    a warning comes with them — was terminal to precisely one function.
    Every other guard spelled the terminal set out by hand and omitted it,
    so the 15-minute stale sweep relabelled successful partial runs
    "Timed out", the on-failure hook could overwrite one with "failed", and
    the dedupe in ``shadow_trigger`` refused to re-dispatch the very file
    whose warning said "upload the collar file, then re-run this one".

The tests here are structural on purpose. Both bugs were absences, and an
absence is not something a behavioural test of the happy path can see.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.hatchet_workflows import _progress

APP = Path(__file__).resolve().parents[1] / "app"

#: Modules that legitimately close an ingest run.
COMPLETION_FN = "mark_completed_by_run"

#: Either of these discharges the duty to tell Laravel. `broadcast_terminal`
#: is the wrapper; `post_ingestion_progress` is what it wraps, and the two
#: modules that predate the wrapper call it directly.
NOTIFY_FNS = frozenset({"broadcast_terminal", "post_ingestion_progress"})


def _module_paths() -> list[Path]:
    return sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    """Drop every docstring so a *description* of a construct is not read as
    the construct. Every fix in this codebase leaves a comment or a
    docstring quoting what it replaced; five source-scanning tests in this
    suite have already matched their own explanation."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            node.body = body[1:] or [ast.Pass()]
    return tree


def _called_names(tree: ast.AST) -> set[str]:
    """Every function name called anywhere in the module, attribute or not."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            names.add(func.attr)
        elif isinstance(func, ast.Name):
            names.add(func.id)
    return names


def _string_constants(tree: ast.AST) -> list[str]:
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


class TestEveryCompletionIsAnnounced:
    def test_the_rule_has_subjects(self) -> None:
        """Guards the guard. If the completion helper is ever renamed, the
        scan below silently passes over an empty set and this file starts
        proving nothing."""
        callers = [
            path.name for path in _module_paths()
            if COMPLETION_FN in _called_names(ast.parse(path.read_text(encoding="utf-8")))
            and path.name != "_progress.py"
        ]
        assert len(callers) >= 5, (
            f"expected several {COMPLETION_FN} callers, found {callers}. "
            "Renamed? Then rename it here too — an empty scan is a green test "
            "that checks nothing."
        )

    @pytest.mark.parametrize(
        "path", _module_paths(), ids=lambda p: p.name,
    )
    def test_a_module_that_completes_a_run_also_notifies(self, path: Path) -> None:
        if path.name == "_progress.py":
            return  # defines both sides

        tree = _strip_docstrings(ast.parse(path.read_text(encoding="utf-8")))
        called = _called_names(tree)
        if COMPLETION_FN not in called:
            return

        assert called & NOTIFY_FNS, (
            f"{path.name} closes an ingest run with {COMPLETION_FN}() and never "
            f"tells Laravel. Terminal in Postgres is not terminal in the "
            f"product: without one of {sorted(NOTIFY_FNS)} there is no Reverb "
            f"event, no data_version bump and no MV refresh, so the rows land "
            f"and every surface — map tiles, Overview KPIs, the drillhole page "
            f"— keeps showing what it showed before the upload."
        )


class TestPartialIsTerminal:
    def test_partial_is_in_the_terminal_set(self) -> None:
        assert "partial" in _progress.TERMINAL_STATUSES, (
            "'partial' is written by mark_completed_by_run and allowed by the "
            "ingest_progress CHECK constraint. Leaving it out of this tuple is "
            "what let the stale sweep relabel finished runs 'timed_out'."
        )

    def test_the_sql_fragment_is_derived_from_the_tuple(self) -> None:
        rendered = _progress.TERMINAL_STATUS_SQL
        assert rendered == ",".join(f"'{s}'" for s in _progress.TERMINAL_STATUSES)
        for status in _progress.TERMINAL_STATUSES:
            assert f"'{status}'" in rendered

    def test_no_module_hardcodes_the_terminal_set(self) -> None:
        """The defect was drift between eight copies, not any single copy."""
        stale = "'completed','failed','cancelled','timed_out'"
        stale_spaced = "'completed', 'failed', 'cancelled', 'timed_out'"

        offenders: list[str] = []
        for path in _module_paths():
            tree = _strip_docstrings(ast.parse(path.read_text(encoding="utf-8")))
            for text in _string_constants(tree):
                if "ingest_progress" not in text and "status NOT IN" not in text:
                    continue
                if stale in text or stale_spaced in text:
                    offenders.append(path.name)
                    break

        assert offenders == [], (
            f"{offenders} spell the terminal status set out by hand, omitting "
            "'partial'. Use _progress.TERMINAL_STATUS_SQL — the whole point is "
            "that adding a status is one edit, not nine."
        )

    def test_archive_and_run_terminal_sets_agree_on_partial(self) -> None:
        from app.hatchet_workflows import _archive_progress

        assert "partial" in _archive_progress.TERMINAL_STATUSES
        assert "partial" in _progress.TERMINAL_STATUSES


class TestTerminalStatus:
    @pytest.mark.parametrize(
        ("rows_written", "warnings", "expected"),
        [
            (10, [], "completed"),
            (10, None, "completed"),
            (None, [], "completed"),          # "did not say" is not "said zero"
            (None, None, "completed"),
            (0, [], "partial"),
            (0, None, "partial"),
            (10, [{"code": "orphaned_intervals"}], "partial"),
            (None, [{"code": "x"}], "partial"),
        ],
    )
    def test_status(self, rows_written, warnings, expected) -> None:
        assert _progress.terminal_status(
            rows_written=rows_written, warnings=warnings,
        ) == expected

    def test_it_matches_what_mark_completed_writes(self) -> None:
        """mark_completed_by_run must not re-derive the rule."""
        import inspect

        source = inspect.getsource(_progress.mark_completed_by_run)
        tree = _strip_docstrings(ast.parse(source.lstrip()))
        assert "terminal_status" in _called_names(tree), (
            "mark_completed_by_run computes the status inline again. The "
            "broadcast has to name the same status the row was given; two "
            "copies of the rule is two chances to disagree."
        )


class TestTerminalMessage:
    def test_singular_and_plural(self) -> None:
        assert _progress.terminal_message(rows_written=1, warnings=[]) == "1 row written"
        assert _progress.terminal_message(rows_written=2, warnings=[]) == "2 rows written"

    def test_thousands_are_grouped(self) -> None:
        assert _progress.terminal_message(
            rows_written=12345, warnings=[],
        ) == "12,345 rows written"

    def test_the_noun_travels(self) -> None:
        assert _progress.terminal_message(
            rows_written=3, warnings=[], noun="feature",
        ) == "3 features written"

    def test_nothing_written(self) -> None:
        assert _progress.terminal_message(
            rows_written=0, warnings=[], noun="curve",
        ) == "No curves written"

    def test_unreported_count(self) -> None:
        assert _progress.terminal_message(
            rows_written=None, warnings=[],
        ) == "Finished"

    def test_the_first_warning_reaches_the_message(self) -> None:
        detail = (
            "37 row(s) reference a hole_id with no collar in this project. "
            "Upload the collar file, then re-run this one."
        )
        message = _progress.terminal_message(
            rows_written=120,
            warnings=[{"code": "orphaned_intervals", "detail": detail}],
        )
        assert detail in message
        assert message.startswith("120 rows written")

    def test_extra_warnings_are_counted_not_dropped_silently(self) -> None:
        message = _progress.terminal_message(
            rows_written=5,
            warnings=[{"detail": "first"}, {"detail": "second"}, {"detail": "third"}],
        )
        assert "first" in message
        assert "(+2 more)" in message

    def test_code_is_used_when_there_is_no_detail(self) -> None:
        message = _progress.terminal_message(
            rows_written=1, warnings=[{"code": "raster_not_ocred"}],
        )
        assert "raster_not_ocred" in message

    def test_it_fits_the_endpoint_validation(self) -> None:
        """Laravel validates message as max:500. A longer string is a 422,
        and a 422 here is a notification that never happens."""
        message = _progress.terminal_message(
            rows_written=1, warnings=[{"detail": "x" * 4000}],
        )
        assert len(message) <= 500
