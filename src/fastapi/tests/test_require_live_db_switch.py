"""REQUIRE_LIVE_DB turns a skipped gate into a failed one (tests/_live_db.py).

The jobs ``integration-postgres`` and ``cron-sweeps-app-role`` stand a Postgres
up and migrate it for the live-database modules. Those modules skip when the
database is missing, which is right on a laptop and wrong there: a service that
did not start, a ``db:apply-raw`` that applied nothing, or a renamed table made
the whole job a column of "s" and ``pytest`` exited 0.

Three layers here, each able to fail on its own:

1. the pure functions, on real ``TestReport`` / ``CollectReport`` objects;
2. a nested ``pytest`` over a throwaway directory, which proves the hooks are
   wired into a real run for every way a test can skip (in the test, in a
   fixture, at module level) and that the allow-list and the default still hold;
3. a nested ``pytest`` over the REAL gate modules the audit named, pointed at no
   database at all: with the switch they fail, without it they skip.

No database is needed anywhere in this file.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from _pytest import reports as pytest_reports  # not `TestReport`: a Test* name would be collected

from tests import _live_db

FASTAPI_ROOT = Path(__file__).resolve().parents[1]

#: A DSN nothing listens on: the connect is refused at once, no 5 s timeout.
DEAD_DSN = "postgresql://nobody:nothing@127.0.0.1:1/nowhere"


# --------------------------------------------------------------------------- #
# 1. The pure functions
# --------------------------------------------------------------------------- #


def _skipped_test_report(reason: str, *, xfail: bool = False) -> pytest_reports.TestReport:
    report = pytest_reports.TestReport(
        nodeid="tests/test_x.py::test_y",
        location=("tests/test_x.py", 3, "test_y"),
        keywords={},
        outcome="skipped",
        longrepr=("tests/test_x.py", 3, f"Skipped: {reason}"),
        when="setup",
    )
    if xfail:
        report.wasxfail = "known bug"
    return report


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_switch_values_that_turn_it_on(value):
    assert _live_db.live_db_required({"REQUIRE_LIVE_DB": value}) is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_switch_values_that_leave_it_off(value):
    assert _live_db.live_db_required({"REQUIRE_LIVE_DB": value}) is False


def test_switch_is_off_when_unset():
    assert _live_db.live_db_required({}) is False


def test_skip_reason_is_read_from_a_real_skipped_report():
    report = _skipped_test_report("no Postgres at PG_DSN (refused)")
    assert _live_db.skip_reason(report) == "no Postgres at PG_DSN (refused)"


def test_a_passed_report_has_no_skip_reason():
    report = pytest_reports.TestReport(
        nodeid="t", location=("t.py", 1, "t"), keywords={}, outcome="passed",
        longrepr=None, when="call",
    )
    assert _live_db.skip_reason(report) is None


def test_an_expected_failure_is_not_a_skip():
    # pytest reports an xfail that failed as outcome "skipped" + wasxfail.
    report = _skipped_test_report("known bug", xfail=True)
    assert _live_db.skip_reason(report) is None


def test_enforce_fails_a_skip_when_the_switch_is_on():
    report = _skipped_test_report("relation silver.reports missing; run migrate first")

    changed = _live_db.enforce(report, {"REQUIRE_LIVE_DB": "1"})

    assert changed is True
    assert report.outcome == "failed"
    assert "SKIPPED" in report.longrepr
    assert "relation silver.reports missing; run migrate first" in report.longrepr


def test_enforce_leaves_a_skip_alone_when_the_switch_is_off():
    report = _skipped_test_report("no Postgres")

    assert _live_db.enforce(report, {}) is False
    assert report.outcome == "skipped"


@pytest.mark.parametrize("reason", sorted(_live_db.ALLOWED_SKIP_REASONS))
def test_enforce_tolerates_the_allow_listed_data_conditional_skips(reason):
    report = _skipped_test_report(reason)

    assert _live_db.enforce(report, {"REQUIRE_LIVE_DB": "1"}) is False
    assert report.outcome == "skipped"


def test_the_allow_list_is_matched_exactly_not_by_substring():
    report = _skipped_test_report("No target_models rows in this DB (and Postgres is down)")

    assert _live_db.enforce(report, {"REQUIRE_LIVE_DB": "1"}) is True
    assert report.outcome == "failed"


def test_enforce_does_not_touch_an_expected_failure():
    report = _skipped_test_report("known bug", xfail=True)

    assert _live_db.enforce(report, {"REQUIRE_LIVE_DB": "1"}) is False
    assert report.outcome == "skipped"


def test_enforce_fails_a_module_level_skip_too():
    report = pytest_reports.CollectReport(
        nodeid="tests/test_x.py",
        outcome="skipped",
        longrepr=("tests/test_x.py", 9, "Skipped: postgres env not configured"),
        result=None,
    )

    assert _live_db.enforce(report, {"REQUIRE_LIVE_DB": "1"}) is True
    assert report.outcome == "failed"


# --------------------------------------------------------------------------- #
# 2. A nested pytest over a throwaway directory
# --------------------------------------------------------------------------- #


def _nested_pytest(
    target: Path, *args: str, cwd: Path, switch: str | None, extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST", "REQUIRE_LIVE_DB"}
    }
    env["PYTHONPATH"] = os.pathsep.join([str(FASTAPI_ROOT), env.get("PYTHONPATH", "")])
    if switch is not None:
        env["REQUIRE_LIVE_DB"] = switch
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "--no-header", *args, str(target)],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=180, check=False,
    )


_WORDS = {
    "passed": "passed", "failed": "failed", "skipped": "skipped", "deselected": "deselected",
    "error": "error", "errors": "error", "warning": "warnings", "warnings": "warnings",
}


def _counts(stdout: str) -> dict[str, int]:
    """Outcome counts from pytest's last line ('3 failed, 1 passed in 0.04s').

    Counted from the summary rather than by searching the text: a skip reason
    can legitimately contain the word "failed" ("Connect call failed ...").
    """
    last = [line for line in stdout.strip().splitlines() if line.strip()][-1]
    return {
        _WORDS[word]: int(n)
        for n, word in re.findall(r"(\d+) (passed|failed|skipped|errors?|deselected|warnings?)\b", last)
    }


@pytest.fixture
def scratch_suite(tmp_path: Path) -> Path:
    (tmp_path / "test_gate.py").write_text(textwrap.dedent('''
        import pytest

        @pytest.fixture
        def db():
            pytest.skip("no Postgres at PG_DSN (fixture)")

        def test_skips_in_the_body():
            pytest.skip("relation silver.reports missing")

        def test_skips_in_a_fixture(db):
            raise AssertionError("unreachable")

        @pytest.mark.skipif(True, reason="skipif marker")
        def test_skips_by_marker():
            raise AssertionError("unreachable")

        def test_passes():
            assert True
    '''))
    return tmp_path


def test_nested_run_without_the_switch_just_skips(scratch_suite):
    result = _nested_pytest(scratch_suite, "-rs", cwd=scratch_suite, switch=None, extra_env={})
    # `-p tests._live_db` is deliberately absent: this is the default every
    # other job and every laptop gets.
    assert result.returncode == 0, result.stdout + result.stderr
    assert _counts(result.stdout) == {"passed": 1, "skipped": 3}


def test_nested_run_with_the_switch_fails_every_kind_of_skip(scratch_suite):
    result = _nested_pytest(
        scratch_suite, "-p", "tests._live_db", cwd=scratch_suite, switch="1",
    )

    out = result.stdout
    assert result.returncode == 1, out + result.stderr
    # A skip in the test body is a FAILED test; one in a fixture or a marker
    # fires during setup, which pytest counts as an ERROR. Either way it is red.
    assert _counts(out) == {"failed": 1, "error": 2, "passed": 1}, out
    # The skip's own reason survives into the failure, so the log says what is missing.
    assert "relation silver.reports missing" in out
    assert "no Postgres at PG_DSN (fixture)" in out
    assert "skipif marker" in out
    assert "REQUIRE_LIVE_DB=1" in out


def test_nested_run_with_the_switch_aborts_on_a_module_level_skip(tmp_path):
    (tmp_path / "test_module_skip.py").write_text(textwrap.dedent('''
        import pytest
        pytest.skip("postgres env not configured", allow_module_level=True)

        def test_never_collected():
            pass
    '''))

    result = _nested_pytest(tmp_path, "-p", "tests._live_db", cwd=tmp_path, switch="1")

    assert result.returncode != 0, result.stdout
    assert "postgres env not configured" in result.stdout
    assert _counts(result.stdout).get("error") == 1, result.stdout


def test_nested_run_with_the_switch_tolerates_the_allow_listed_skips(tmp_path):
    (tmp_path / "test_data_conditional.py").write_text(textwrap.dedent('''
        import pytest

        def test_no_rows():
            pytest.skip("No target_models rows in this DB")

        def test_no_delivery():
            pytest.skip("RedStar delivery not present")

        def test_runs():
            assert True
    '''))

    result = _nested_pytest(tmp_path, "-p", "tests._live_db", cwd=tmp_path, switch="1")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _counts(result.stdout) == {"passed": 1, "skipped": 2}


# --------------------------------------------------------------------------- #
# 3. The real gate modules, with no database to find
# --------------------------------------------------------------------------- #

#: The modules the audit named. Each guards itself with a skip that means "the
#: database is not there": a dead PG_DSN for the first two, and for the third a
#: missing POSTGRES_USER, which is what a job that lost its `env:` block
#: looks like.
GATE_MODULES = (
    "tests/test_cron_sweeps_under_app_role.py",
    "tests/test_workspace_export_rls_scope.py",
    "tests/test_embed_pending_completion_sweep.py",
)


def test_the_named_gate_modules_fail_instead_of_skipping_when_the_database_is_missing():
    """One nested run per mode over all three modules (each run imports the app,
    so doing this per module would cost several seconds apiece).

    --continue-on-collection-errors: the embed module skips at import, which with
    the switch is a collection error; without it the other two would never run.
    """
    env = {"PG_DSN": DEAD_DSN, "PG_APP_ROLE": "georag_app", "POSTGRES_USER": ""}
    args = ("-m", "integration", "-rsfE", "--continue-on-collection-errors")
    targets = [str(FASTAPI_ROOT / module) for module in GATE_MODULES]

    without_switch = _nested_pytest(
        targets[0], *args, *targets[1:], cwd=FASTAPI_ROOT, switch=None, extra_env=env,
    )
    with_switch = _nested_pytest(
        targets[0], *args, *targets[1:], cwd=FASTAPI_ROOT, switch="1", extra_env=env,
    )

    # Without the switch every gate skips and the run is green: that is the hole.
    plain = _counts(without_switch.stdout)
    assert without_switch.returncode == 0, without_switch.stdout + without_switch.stderr
    assert plain.get("skipped", 0) >= len(GATE_MODULES), plain
    assert not plain.get("failed") and not plain.get("error"), plain

    # With it, nothing is left skipped, the run is red, and every named module
    # is on the list of things that failed.
    strict = _counts(with_switch.stdout)
    assert with_switch.returncode != 0, with_switch.stdout + with_switch.stderr
    assert not strict.get("skipped"), strict
    red_lines = [
        line for line in with_switch.stdout.splitlines() if line.startswith(("FAILED ", "ERROR "))
    ]
    for module in GATE_MODULES:
        assert any(module in line for line in red_lines), (module, red_lines)
    assert "REQUIRE_LIVE_DB=1" in with_switch.stdout
