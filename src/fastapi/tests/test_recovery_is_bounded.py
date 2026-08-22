"""Automatic recovery has to stop, and has to stop small.

Three self-healing mechanisms sit on the ingestion path. Each was written
to close a real incident, and each was written without a bound -- which is
the failure mode automatic recovery has instead of the one it prevents.

  orphan_sweep         minted a fresh ingest_progress row every ten
                       minutes, forever, for any document holding a
                       passage that can never embed. Roughly twelve dead
                       rows a day, each firing a Reverb `timed_out`
                       broadcast the Ingestion Runs UI rendered as a
                       failed ingestion the user never started. An alarm
                       that repeats forever is not an alarm.

  qdrant bootstrap     awaited a subprocess with no timeout. It talks to
                       the same Qdrant whose emptiness triggered the heal,
                       so "reachable but not serving" hung it for the
                       task's whole 2 h execution_timeout while every
                       inline embed dispatch queued behind it.

  qdrant drift reset   nulled every embedding_id in every workspace in one
                       statement, on the strength of one count() returning
                       zero -- a reading a slow restore produces too. The
                       bill for a false positive was a full corpus
                       re-embed.

These are source-level and constant-level checks rather than behavioural
ones: all three paths need a live Postgres and a live Qdrant to exercise,
and the property that matters is that a bound EXISTS at all.
"""
from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path

import pytest

from app.hatchet_workflows import _progress
from app.services.ingest import orphan_sweep

WORKFLOWS = Path(__file__).resolve().parent.parent / "app" / "hatchet_workflows"


class TestTheRecoveryAttemptCap:
    def test_one_definition_serves_both_sweeps(self) -> None:
        """stale_run_detector had a cap and orphan_sweep did not. Two
        readings of one env var, each with its own default, is how they
        would have drifted apart again."""
        detector = (WORKFLOWS / "stale_run_detector.py").read_text(
            encoding="utf-8")

        assert "return ingest_progress.recovery_max_attempts()" in detector
        assert detector.count('os.environ.get("STALE_RUN_RECOVERY_MAX_ATTEMPTS"') == 0

    def test_the_default_allows_the_original_plus_two_retries(self) -> None:
        assert _progress.recovery_max_attempts() == 3

    def test_it_is_tunable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STALE_RUN_RECOVERY_MAX_ATTEMPTS", "7")
        assert _progress.recovery_max_attempts() == 7

    @pytest.mark.parametrize("raw", ["0", "-1", "", "three", "3.5"])
    def test_a_nonsense_value_falls_back_rather_than_disabling_the_cap(
        self, raw: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`0` is the dangerous one: read literally it means "never
        recover", but a typo'd env var must not silently change policy in
        either direction."""
        monkeypatch.setenv("STALE_RUN_RECOVERY_MAX_ATTEMPTS", raw)
        assert _progress.recovery_max_attempts() == 3

    def test_the_env_var_is_read_in_exactly_one_place(self) -> None:
        source = inspect.getsource(_progress)
        assert source.count("STALE_RUN_RECOVERY_MAX_ATTEMPTS") == 1

    def test_orphan_sweep_reads_the_attempt_number_it_carries(self) -> None:
        """`parent_attempt_number` was selected by the SQL, assigned onto
        the dataclass, and then never read — which is why the sweep looped.
        Being carried is not being used."""
        source = inspect.getsource(orphan_sweep.create_recovery_run)

        assert "parent_attempt_number" in source
        assert "recovery_max_attempts()" in source

    def test_giving_up_is_logged_loudly_enough_to_find(self) -> None:
        """The whole point is that the document surfaces ONCE. If the
        exhausted branch logged at debug, it would go from twelve alarms a
        day to none."""
        source = inspect.getsource(orphan_sweep.create_recovery_run)

        tree = ast.parse(source)
        levels = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "log"
        }

        assert "warning" in levels or "error" in levels


class TestTheQdrantSelfHealBounds:
    @staticmethod
    def _source() -> str:
        return (WORKFLOWS / "embed_pending_passages.py").read_text(
            encoding="utf-8")

    def test_the_bootstrap_subprocess_cannot_run_forever(self) -> None:
        source = self._source()

        assert "_QDRANT_BOOTSTRAP_TIMEOUT_S" in source
        assert "_aio.wait_for(" in source
        assert "_proc.kill()" in source, (
            "wait_for cancels the await but leaves the process running; a "
            "hung init_qdrant.py would survive the sweep that spawned it"
        )

    def test_a_timed_out_bootstrap_does_not_reset_anything(self) -> None:
        """The dangerous ordering. A bootstrap that hung is the LEAST
        safe moment to null embedding_id: Qdrant is reachable but not
        serving, which is also what a slow restore looks like."""
        source = self._source()

        timeout_at = source.index("_QDRANT_BOOTSTRAP_TIMEOUT_S,")
        reset_at = source.index("SET embedding_id = NULL")
        guard_at = source.index("if _proc.returncode == 0:")

        assert timeout_at < guard_at < reset_at

    def test_the_bootstrap_cap_is_far_below_the_task_timeout(self) -> None:
        from app.hatchet_workflows import embed_pending_passages as mod

        assert 0 < mod._QDRANT_BOOTSTRAP_TIMEOUT_S <= 600, (
            "the point of the cap is that it fires long before the task's "
            "2 h execution_timeout, which is what it used to block for"
        )

    def test_the_drift_reset_is_capped(self) -> None:
        from app.hatchet_workflows import embed_pending_passages as mod

        assert mod._QDRANT_DRIFT_RESET_BATCH > 0

        source = self._source()
        reset = source[source.index("SET embedding_id = NULL"):][:600]

        assert "LIMIT $1" in reset, (
            "the reset is unbounded again — one count() reading zero would "
            "null every embedding_id in every workspace"
        )
        assert "ORDER BY" in reset, (
            "without an order the batches re-pick arbitrary rows each sweep "
            "and a genuine wipe may never fully recover"
        )

    def test_the_reset_still_recovers_a_real_wipe(self) -> None:
        """Bounding it must not turn a self-heal into a partial heal.

        The drift condition (collection empty, PG says embedded) stays
        true until the collection refills, so successive sweeps take
        successive batches. That only holds while the batch cap is
        smaller than the corpus but not absurdly so — a cap of 1 would
        technically recover, in a year.
        """
        from app.hatchet_workflows import embed_pending_passages as mod

        assert mod._QDRANT_DRIFT_RESET_BATCH >= 1000


def test_no_recovery_path_shells_out_without_a_timeout() -> None:
    """The general rule the bootstrap broke.

    `create_subprocess_exec` followed by a bare `communicate()` hands an
    external process the ability to hold a task open for its entire
    execution_timeout.
    """
    offenders = []

    for path in sorted(WORKFLOWS.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "create_subprocess_exec" not in source:
            continue
        if "wait_for" not in source:
            offenders.append(path.name)

    assert offenders == [], (
        "these workflows spawn a subprocess with no bounded wait:\n  "
        + "\n  ".join(offenders)
    )


def test_the_scan_above_found_the_module_it_was_written_for() -> None:
    source = (WORKFLOWS / "embed_pending_passages.py").read_text(
        encoding="utf-8")

    assert "create_subprocess_exec" in source, (
        "embed_pending_passages no longer spawns the bootstrap — if that is "
        "deliberate, the subprocess scan above now covers nothing"
    )


def test_os_environ_is_not_read_at_import_time_for_the_cap() -> None:
    """Read per call, not frozen at import.

    A module-level `MAX = int(os.environ[...])` cannot be changed without
    a redeploy, and cannot be tested without reimporting the module.
    """
    before = os.environ.get("STALE_RUN_RECOVERY_MAX_ATTEMPTS")
    try:
        os.environ["STALE_RUN_RECOVERY_MAX_ATTEMPTS"] = "9"
        assert _progress.recovery_max_attempts() == 9
    finally:
        if before is None:
            os.environ.pop("STALE_RUN_RECOVERY_MAX_ATTEMPTS", None)
        else:
            os.environ["STALE_RUN_RECOVERY_MAX_ATTEMPTS"] = before


class TestSilentFailuresLeaveTheMachine:
    """Three detectors on this path used to end at a log line.

    Every one of them detects a state PG cannot see. `cost.burn.alert`
    ended as an audit_ledger row and an admin broadcast; the Qdrant
    partial-loss check ended as a `log.warning`; the answer-quality watch
    was the first to get an egress. The platform has exactly one outbound
    route — a Log Analytics scheduled query rule feeding
    `georag-alerts-ag` — and these tests pin that each detector is on it.

    The partial-loss case is the sharpest. Qdrant drops points that
    silver.document_passages records as embedded; the collection is
    non-empty so the all-empty self-heal does not fire; retrieval just
    returns fewer hits. To a user that reads as "the corpus does not
    cover my question", which is a wrong answer, not an outage.
    """

    MARKERS = [
        ("embed_pending_passages", "QDRANT_PARTIAL_LOSS_MARKER"),
        ("cost_burn_watcher", "COST_BURN_ALERT_MARKER"),
        ("answer_quality_watch", "ALERT_MARKER"),
    ]

    @pytest.mark.parametrize(("module_name", "attr"), MARKERS)
    def test_the_marker_is_greppable(self, module_name: str, attr: str) -> None:
        """Matched with `Log_s has '<marker>'`. A short or lower-case
        marker matches ordinary log prose and the alert fires on noise."""
        import importlib

        module = importlib.import_module(f"app.hatchet_workflows.{module_name}")
        marker = getattr(module, attr)

        assert marker.isupper()
        assert "_" in marker
        assert len(marker) > 12

    @pytest.mark.parametrize(("module_name", "attr"), MARKERS)
    def test_an_alert_rule_matches_it(self, module_name: str, attr: str) -> None:
        """The marker and the rule live in different files and drift
        silently: the line keeps being written and nothing listens."""
        import importlib

        module = importlib.import_module(f"app.hatchet_workflows.{module_name}")
        marker = getattr(module, attr)

        repo = Path(__file__).resolve().parents[3]
        script = (
            repo / "deploy" / "azure" / "alerts" / "create-alerts.sh"
        ).read_text(encoding="utf-8")

        assert marker in script, (
            f"{module_name} logs {marker} and no scheduled query rule "
            "matches it, so the detection stays on the machine"
        )

    @pytest.mark.parametrize(
        ("module_name", "attr"),
        [
            ("embed_pending_passages", "QDRANT_PARTIAL_LOSS_MARKER"),
            ("cost_burn_watcher", "COST_BURN_ALERT_MARKER"),
        ],
    )
    def test_the_marker_is_logged_at_error(
        self, module_name: str, attr: str,
    ) -> None:
        """Level, not just presence.

        The partial-loss line was a `log.warning`. For a gap PG cannot
        see — the passages are unreachable and every record says they are
        fine — a warning in an unwatched stream is the same as no
        detection. The scheduled query rule matches on the marker either
        way, so the level is about what a human reading the log sees,
        which is the fallback when the rule has not been applied yet.

        Asked of the AST rather than the source text: the previous
        version of this test compared a multi-line literal and fell back
        to a loose substring, so it would have passed on any
        reformatting.
        """
        import ast
        import importlib
        import inspect

        module = importlib.import_module(f"app.hatchet_workflows.{module_name}")
        tree = ast.parse(inspect.getsource(module))

        levels = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if not isinstance(node.func.value, ast.Name):
                continue
            if node.func.value.id not in {"log", "logger"}:
                continue
            # The marker is passed as an argument, not interpolated.
            names = {
                arg.id for arg in node.args if isinstance(arg, ast.Name)
            }
            if attr in names:
                levels.add(node.func.attr)

        assert levels, f"{attr} is never passed to a log call"
        assert levels == {"error"}, (
            f"{module_name} logs {attr} at {sorted(levels)}; it must be "
            "error — this is a state nothing else reports"
        )
