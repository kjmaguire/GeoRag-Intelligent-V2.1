"""A Hatchet cron inside the nightly shutdown window can never fire.

WHAT THE WINDOW ACTUALLY IS
    ``deploy/azure/containerapps/scripts/shutdown-sweep.sh`` scales EIGHT
    container apps to ``--min-replicas 0`` and then stops the Flexible
    Server. ``hatchet-worker-cc`` is one of the eight. So during the
    window there is no worker at all, and a cron scheduled inside it does
    not run late -- it does not run.

    The two jobs fire at ``0 6,7 * * *`` and ``0 13,14 * * *`` UTC, with a
    DST guard inside each script dropping whichever hour is not the right
    local time. Both candidate hours therefore have to be treated as
    closed: the window is 06:00-14:00 UTC year-round from a scheduler's
    point of view, even though on any given day it is seven hours, not
    eight.

WHY THIS IS A TEST AND NOT A ONE-TIME FIX
    It has already been got wrong twice, in opposite directions.

    The window used to be 00:00-10:00 UTC. Workflows were moved out of it
    -- ``enrich_passage_context`` carries a comment explaining that 10:30
    "is after the server is back and before the backups (11:00 / 11:30)",
    which was true. On 2026-08-21 the window moved to Pacific time, and
    10:30 UTC became the middle of it. The reasoning did not rot; the
    ground moved under it.

    So the window is READ FROM THE JOB YAML here rather than hardcoded.
    Move the schedule again and this test moves with it, and tells you
    which workflows to move too.

WHAT IS EXEMPT, AND WHY
    A cron that fires many times a day (``*/10 * * * *``, ``0 * * * *``)
    is not scheduled INSIDE the window -- it is scheduled everywhere, and
    losing the ticks that land in the window is the design. Only a cron
    with a fixed hour is checked.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
CONTAINERAPPS = REPO / "deploy" / "azure" / "containerapps"
WORKFLOWS = Path(__file__).resolve().parent.parent / "app" / "hatchet_workflows"

#: Workflows that legitimately sit inside the window. Empty on purpose:
#: nothing can run without a worker, so there is no such thing as a
#: legitimate exception. Kept as the place to record one WITH a reason if
#: the sweep ever stops scaling hatchet-worker-cc to zero.
EXEMPT: dict[str, str] = {}


def _cron_hours(yaml_path: Path) -> set[int]:
    """The hours a Container Apps Job's cronExpression fires at."""
    text = yaml_path.read_text(encoding="utf-8")
    match = re.search(r'cronExpression:\s*"([^"]+)"', text)
    assert match, f"no cronExpression in {yaml_path.name}"
    fields = match.group(1).split()
    assert len(fields) == 5, f"unexpected cron shape in {yaml_path.name}"
    return {int(part) for part in fields[1].split(",")}


def shutdown_window() -> tuple[int, int]:
    """(first closed hour, first open hour) in UTC, from the job YAMLs.

    Both DST candidate hours count as closed, so the returned span is the
    widest the window is ever open -- which is the only safe thing for a
    schedule that cannot know which side of a DST boundary it will run on.
    """
    stop = min(_cron_hours(CONTAINERAPPS / "shutdown-job.yaml"))
    start = max(_cron_hours(CONTAINERAPPS / "startup-job.yaml"))
    return stop, start


def daily_crons() -> list[tuple[str, str, int, int]]:
    """(module, cron expression, hour, minute) for fixed-hour crons only."""
    found: list[tuple[str, str, int, int]] = []
    pattern = re.compile(r"on_crons\s*=\s*\[([^\]]*)\]", re.S)

    for path in sorted(WORKFLOWS.glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for block in pattern.findall(text):
            for expression in re.findall(r'["\']([^"\']+)["\']', block):
                fields = expression.split()
                if len(fields) != 5:
                    continue
                minute, hour = fields[0], fields[1]
                # "*" or "*/N" in the hour field means it fires all day.
                if hour == "*" or hour.startswith("*/"):
                    continue
                try:
                    hour_value = int(hour.split(",")[0])
                    minute_value = 0 if minute.startswith("*") else int(
                        minute.split(",")[0])
                except ValueError:
                    continue
                found.append((path.name, expression, hour_value, minute_value))
    return found


def test_the_window_is_readable_from_the_job_yaml() -> None:
    """Guards the guard: if this stops parsing, every assertion below
    passes vacuously."""
    stop, start = shutdown_window()

    assert 0 <= stop < 24 and 0 <= start < 24
    assert stop < start, (
        f"the shutdown window reads as {stop:02d}:00-{start:02d}:00, which "
        "does not span midnight in the direction this test assumes — "
        "re-derive it before trusting the results below"
    )


def test_some_daily_crons_were_found() -> None:
    crons = daily_crons()
    assert len(crons) >= 10, (
        f"only {len(crons)} fixed-hour crons found — the scan is probably "
        f"broken: {crons}"
    )


def test_no_daily_cron_fires_while_the_worker_is_scaled_to_zero() -> None:
    stop, start = shutdown_window()

    offenders = [
        (module, expression, f"{hour:02d}:{minute:02d} UTC")
        for module, expression, hour, minute in daily_crons()
        if stop <= hour < start and module not in EXEMPT
    ]

    assert not offenders, (
        f"These crons fire between {stop:02d}:00 and {start:02d}:00 UTC, "
        "when shutdown-sweep.sh has scaled hatchet-worker-cc to zero and "
        "stopped the Flexible Server. They do not run late; they do not "
        "run:\n"
        + "\n".join(
            f"  {module:38s} {expression:16s} {when}"
            for module, expression, when in sorted(offenders)
        )
        + f"\n\nMove them after {start:02d}:00 UTC. Both DST candidate "
          "hours count as closed — the sweep fires at two hours and drops "
          "one at runtime, so a schedule cannot know which.\n"
          "If the sweep genuinely stopped scaling the worker down, record "
          "that in EXEMPT with a date and a reason rather than deleting "
          "this test."
    )


def test_the_sweep_still_scales_the_worker_to_zero() -> None:
    """The premise of the test above.

    If hatchet-worker-cc ever leaves the sweep's app list, crons inside
    the window become merely unable to reach Postgres rather than unable
    to start — a different, smaller problem, and this file would be
    overstating it.
    """
    sweep = (CONTAINERAPPS / "scripts" / "shutdown-sweep.sh").read_text(
        encoding="utf-8")

    assert "hatchet-worker-cc" in sweep
    assert "--min-replicas 0" in sweep


@pytest.mark.parametrize("name", ["shutdown-job.yaml", "startup-job.yaml"])
def test_both_jobs_still_fire_at_two_candidate_hours(name: str) -> None:
    """The DST guard depends on it. One hour would mean the window drifts
    an hour twice a year, silently."""
    hours = _cron_hours(CONTAINERAPPS / name)

    assert len(hours) == 2, (
        f"{name} fires at {sorted(hours)}; the DST guard inside the script "
        "expects two candidate hours and drops one at runtime"
    )
    assert max(hours) - min(hours) == 1
