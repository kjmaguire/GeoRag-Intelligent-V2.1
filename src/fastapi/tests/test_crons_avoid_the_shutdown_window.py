"""A Hatchet cron inside the nightly shutdown window can never fire.

WHAT THE WINDOW ACTUALLY IS
    ``deploy/aws/scheduler/shutdown-sweep.sh`` scales every ECS service to
    ``--desired-count 0`` and then stops the RDS instance. The Hatchet
    worker is one of them. So during the window there is no worker at all,
    and a cron scheduled inside it does not run late -- it does not run.

    Rewritten 2026-09-08 for ADR-0022. The window is now scheduled in a
    NAMED TIMEZONE (EventBridge Scheduler), not in UTC. Since 2026-09-16
    it runs 17:00 to 08:30 America/Vancouver -- fifteen and a half hours
    closed, to fit the platform inside a business day and a $100/month
    credit. Hatchet crons are UTC, so the window's UTC position still
    moves with DST -- 00:00-15:30 in PDT, 01:00-16:30 in PST -- and both
    are therefore treated as closed, leaving 16:30-00:00 UTC as the span
    a fixed-hour cron can sit in. The times are read from the Terraform
    below, so do not trust this paragraph over shutdown_window().

    THE SWEEP FIRING IS NOT THE PLATFORM BEING UP, which is the distinction
    this file missed until 2026-09-16 and STARTUP_GRACE_MINUTES now carries.
    Startup was `cron(0 9 * * ? *)`, which in PST is 17:00 UTC exactly --
    the same instant as four crons at `0 17 * * *`. The arithmetic here said
    the window closed at 17:00 and those crons fired at 17:00, so nothing
    was flagged; in reality RDS had not started and the Hatchet engine that
    creates cron runs did not exist, and four workflows would have silently
    not run from November to March. Comparing whole hours is what hid it:
    the sweep's hour and the cron's hour were the same number.

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

#: How long after the startup sweep FIRES before a cron tick can be real.
#:
#: startup-sweep.sh runs `rds start-db-instance`, then blocks on `rds wait
#: db-instance-available`, and only then scales tier 1 -- redis, qdrant, the
#: Hatchet ENGINE, sparse -- and waits for `services-stable`. The engine is
#: what creates a run when a cron ticks, so until tier 1 is stable a tick
#: produces nothing at all. It is not queued for later; there is no
#: scheduler running to queue it.
#:
#: THIS NUMBER IS AN ESTIMATE. Nothing has been deployed on this account, so
#: the dominant term -- RDS cold start -- has never been observed here.
#: Thirty minutes is what `startup_cron` was moved to buy on 2026-09-16
#: (variables.tf carries the reasoning and the cost). If a real morning sweep
#: is measured taking longer, this is the constant to raise, and raising it
#: is what will name the crons that have to move with it.
STARTUP_GRACE_MINUTES = 30


def _local_minutes(variable: str) -> int:
    """Minutes past local midnight a Terraform cron variable's default fires.

    Reads `cron(<minute> <hour> ...)` out of the variable's default in
    variables.tf, which is the same string EventBridge Scheduler is given.
    MINUTES, not hours: `startup_cron` is `cron(30 8 * * ? *)`, and an
    hour-granular read of that is wrong by exactly the margin it exists to
    create.
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
    return int(fields[1]) * 60 + int(fields[0])


def shutdown_window() -> tuple[int, int]:
    """(first closed minute, first USABLE minute) in UTC, from the Terraform.

    Both as minutes past midnight UTC. Both DST offsets count as closed, so
    the returned span is the widest the window is ever open -- the only safe
    thing for a schedule that cannot know which side of a DST boundary it
    will run on.

    The second element is when the platform can RUN something, not when the
    sweep fires: STARTUP_GRACE_MINUTES is added. A cron at exactly that
    minute is legal and is the tightest legal slot there is.
    """
    stop_local = _local_minutes("shutdown_cron")
    start_local = _local_minutes("startup_cron")
    stop = min((stop_local + off * 60) % 1440 for off in _PACIFIC_OFFSETS)
    start = max((start_local + off * 60) % 1440 for off in _PACIFIC_OFFSETS)
    return stop, start + STARTUP_GRACE_MINUTES


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


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

    assert 0 <= stop < 1440 and 0 <= start < 1440
    assert stop < start, (
        f"the shutdown window reads as {_hhmm(stop)}-{_hhmm(start)} UTC, "
        "which does not span midnight in the direction this test assumes — "
        "re-derive it before trusting the results below"
    )


def test_the_sweep_gets_a_head_start_on_the_first_cron() -> None:
    """The regression this file exists to not have again.

    A cron may sit exactly on the first usable minute; it may not sit on the
    minute the sweep FIRES. Those were the same number until 2026-09-16,
    which is why four workflows sat on an instant where nothing could run
    them and this file said it was fine.
    """
    assert STARTUP_GRACE_MINUTES > 0, (
        "with no grace, the first usable minute is the minute RDS is asked "
        "to start — see this module's docstring"
    )

    fires_at = max(
        (_local_minutes("startup_cron") + off * 60) % 1440
        for off in _PACIFIC_OFFSETS
    )
    earliest = min(
        (hour * 60 + minute for _, _, hour, minute in daily_crons()),
        default=None,
    )
    assert earliest is not None, "no fixed-hour crons found — see the scan above"

    # Deliberately NOT the offender test's comparison. That one asks whether
    # each cron clears the grace; this asks whether the earliest one clears
    # the sweep at all, which is the weaker property that was silently false.
    assert earliest > fires_at, (
        f"the earliest fixed-hour cron is at {_hhmm(earliest)} UTC and the "
        f"startup sweep fires at {_hhmm(fires_at)} UTC in the worst DST "
        "half of the year. Equal is not safe — the sweep has to start RDS, "
        "wait for it, and bring tier 1 stable before the Hatchet engine "
        "that creates a cron run exists at all."
    )


def test_some_daily_crons_were_found() -> None:
    crons = daily_crons()
    assert len(crons) >= 10, (
        f"only {len(crons)} fixed-hour crons found — the scan is probably "
        f"broken: {crons}"
    )


def test_no_daily_cron_fires_while_the_worker_is_scaled_to_zero() -> None:
    stop, usable = shutdown_window()

    offenders = [
        (module, expression, f"{hour:02d}:{minute:02d} UTC")
        for module, expression, hour, minute in daily_crons()
        if stop <= hour * 60 + minute < usable and module not in EXEMPT
    ]

    assert not offenders, (
        f"These crons fire between {_hhmm(stop)} and {_hhmm(usable)} UTC, "
        "when shutdown-sweep.sh has scaled the Hatchet worker to zero and "
        "stopped the RDS instance, or when the startup sweep is still "
        "bringing it back. They do not run late; they do not run:\n"
        + "\n".join(
            f"  {module:38s} {expression:16s} {when}"
            for module, expression, when in sorted(offenders)
        )
        + f"\n\nMove them to {_hhmm(usable)} UTC or later. Both DST "
          "candidate hours count as closed — the window is scheduled in "
          "local time and these crons are UTC, so a schedule cannot know "
          "which side of a transition it will run on. The last "
          f"{STARTUP_GRACE_MINUTES} minutes of that span are the startup "
          "sweep's own head start, not the window: see "
          "STARTUP_GRACE_MINUTES.\n"
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

    # An ALLOW-LIST, not a single literal. _PACIFIC_OFFSETS above encodes
    # UTC-7/UTC-8 and the North American DST transition dates, so what this
    # has to assert is "the zone is Pacific", not "the zone is spelled
    # America/Los_Angeles". Vancouver moved here on 2026-09-16 and is the
    # same clock to the second: same offsets, same transitions, different
    # name for where the operator actually is.
    #
    # Adding to this list is only safe for a zone that shares BOTH offsets
    # AND both transition dates. America/Phoenix is the trap — it is
    # nominally Mountain, never observes DST, and would silently make every
    # offset above wrong for half the year.
    pacific = ("America/Los_Angeles", "America/Vancouver", "America/Tijuana")
    assert any(f'default     = "{z}"' in block.group(1) for z in pacific), (
        "the maintenance window's timezone is not one of the US/Canada "
        f"Pacific zones {pacific}; _PACIFIC_OFFSETS above is derived from "
        "Pacific time and must change with it"
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
    minute_field, hour_field = match.group(1).split()[:2]

    assert "," not in hour_field and "/" not in hour_field, (
        f"{variable} fires at {hour_field!r}. EventBridge Scheduler is "
        "timezone-aware, so a single hour is correct; multiple hours means "
        "the Azure double-fire has come back without the guard that made it "
        "safe."
    )
    # The minute field is read the same way since 2026-09-16, both here and
    # by scheduler.tf's tonumber() — a "*/15" there is a plan error at best
    # and a silently wrong alert-suppression period at worst.
    assert "," not in minute_field and "*" not in minute_field, (
        f"{variable} fires at minute {minute_field!r}. One sweep, one fire "
        "time: the window's length is derived from these two fields."
    )
