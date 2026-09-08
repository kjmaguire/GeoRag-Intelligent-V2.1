"""A Hatchet cron inside the nightly shutdown window can never fire.

WHAT THE WINDOW ACTUALLY IS
    ``deploy/aws/scheduler/shutdown-sweep.sh`` scales every ECS service to
    ``--desired-count 0`` and then stops the RDS instance. The Hatchet
    worker is one of them. So during the window there is no worker at all,
    and a cron scheduled inside it does not run late -- it does not run.

    Rewritten 2026-09-08 for ADR-0022. The window is now scheduled in a
    NAMED TIMEZONE (EventBridge Scheduler), not in UTC: 23:00 to 06:00
    US-Pacific. Hatchet crons are UTC, so the window's UTC position still
    moves with DST -- 06:00-13:00 in PDT, 07:00-14:00 in PST -- and both
    are therefore treated as closed. That is the same conservative span
    the Azure version computed, arrived at from the timezone rather than
    from a pair of double-fire cron hours.

    What is GONE is the reason there were two cron hours to read: Container
    Apps Jobs had no timezone support, so each sweep fired at both
    candidates with an in-script DST guard exiting 0 on the wrong one.

WHY THIS IS A TEST AND NOT A ONE-TIME FIX
    It has already been got wrong twice, in opposite directions.

    The window used to be 00:00-10:00 UTC. Workflows were moved out of it
    -- ``enrich_passage_context`` carries a comment explaining that 10:30
    "is after the server is back and before the backups (11:00 / 11:30)",
    which was true at the time -- those backup crons were themselves
    deleted on 2026-08-23, so the second half of that sentence now names
    nothing. On 2026-08-21 the window moved to Pacific time, and
    10:30 UTC became the middle of it. The reasoning did not rot; the
    ground moved under it.

    So the window is DERIVED FROM THE TERRAFORM SCHEDULE here rather than
    hardcoded. Move the schedule again and this test moves with it, and
    tells you which workflows to move too.

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
TERRAFORM = REPO / "deploy" / "aws" / "terraform" / "variables.tf"
WORKFLOWS = Path(__file__).resolve().parent.parent / "app" / "hatchet_workflows"

#: Workflows that legitimately sit inside the window. Empty on purpose:
#: nothing can run without a worker, so there is no such thing as a
#: legitimate exception. Kept as the place to record one WITH a reason if
#: the sweep ever stops scaling the Hatchet worker to zero.
EXEMPT: dict[str, str] = {}

#: US-Pacific offsets from UTC. The window is scheduled in local time and
#: Hatchet crons are UTC, so its UTC position moves by an hour twice a
#: year. Both are treated as closed.
_PACIFIC_OFFSETS = (7, 8)  # PDT, PST


def _local_hour(variable: str) -> int:
    """The local-time hour a Terraform cron variable's default fires at.

    Reads `cron(<minute> <hour> ...)` out of the variable's default in
    variables.tf, which is the same string EventBridge Scheduler is given.
    """
    text = TERRAFORM.read_text(encoding="utf-8")
    block = re.search(
        rf'variable\s+"{variable}"\s*\{{(.*?)\n\}}', text, re.S
    )
    assert block, f"no variable {variable!r} in {TERRAFORM.name}"
    match = re.search(r'default\s*=\s*"cron\(([^)]+)\)"', block.group(1))
    assert match, f"no cron default for {variable!r}"
    fields = match.group(1).split()
    assert len(fields) == 6, f"unexpected cron shape for {variable!r}: {fields}"
    return int(fields[1])


def shutdown_window() -> tuple[int, int]:
    """(first closed hour, first open hour) in UTC, from the Terraform.

    Both DST offsets count as closed, so the returned span is the widest
    the window is ever open -- the only safe thing for a schedule that
    cannot know which side of a DST boundary it will run on.
    """
    stop_local = _local_hour("shutdown_cron")
    start_local = _local_hour("startup_cron")
    stop = min((stop_local + off) % 24 for off in _PACIFIC_OFFSETS)
    start = max((start_local + off) % 24 for off in _PACIFIC_OFFSETS)
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


def test_the_window_is_readable_from_the_terraform() -> None:
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
        "when shutdown-sweep.sh has scaled the Hatchet worker to zero and "
        "stopped the RDS instance. They do not run late; they do not "
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

    If the Hatchet worker ever leaves the sweep's service list, crons
    inside the window become merely unable to reach Postgres rather than
    unable to start — a different, smaller problem, and this file would be
    overstating it.
    """
    sweep = (REPO / "deploy" / "aws" / "scheduler" / "shutdown-sweep.sh").read_text(
        encoding="utf-8")

    assert "hatchet-worker" in sweep
    assert "--desired-count 0" in sweep


def test_the_window_is_scheduled_in_a_named_timezone() -> None:
    """The DST double-fire is gone, and this is what replaced it.

    Container Apps Jobs had no timezone support, so each sweep fired at
    TWO candidate UTC hours and an in-script guard exited 0 on the wrong
    one — and that guard compared against midnight UTC rather than the
    real transition instant, so it ran an hour early each March and an
    hour late each November until 2026-08-21.

    EventBridge Scheduler takes a timezone. If that ever reverts to a
    fixed UTC schedule, this window drifts an hour twice a year silently
    and the offsets above stop being the right way to compute it.
    """
    text = TERRAFORM.read_text(encoding="utf-8")
    block = re.search(
        r'variable\s+"maintenance_timezone"\s*\{(.*?)\n\}', text, re.S
    )
    assert block, "no maintenance_timezone variable"
    assert 'default     = "America/Los_Angeles"' in block.group(1), (
        "the maintenance window's timezone changed; _PACIFIC_OFFSETS above "
        "is derived from US-Pacific and must change with it"
    )


@pytest.mark.parametrize("variable", ["shutdown_cron", "startup_cron"])
def test_each_sweep_fires_exactly_once(variable: str) -> None:
    """One schedule, one fire. A comma in the hour field would mean the
    double-fire mechanism had come back without its guard."""
    text = TERRAFORM.read_text(encoding="utf-8")
    block = re.search(rf'variable\s+"{variable}"\s*\{{(.*?)\n\}}', text, re.S)
    assert block
    match = re.search(r'default\s*=\s*"cron\(([^)]+)\)"', block.group(1))
    assert match
    hour_field = match.group(1).split()[1]

    assert "," not in hour_field and "/" not in hour_field, (
        f"{variable} fires at {hour_field!r}. EventBridge Scheduler is "
        "timezone-aware, so a single hour is correct; multiple hours means "
        "the Azure double-fire has come back without the guard that made it "
        "safe."
    )
