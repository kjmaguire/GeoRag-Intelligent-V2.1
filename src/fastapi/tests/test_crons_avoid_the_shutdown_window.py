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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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


# ---------------------------------------------------------------------------
# End times (HAT-14, 2026-09-29)
# ---------------------------------------------------------------------------
# Everything above checks when a cron STARTS. A cron that starts in the open
# window but whose execution budget runs past the stop is killed mid-run:
# shutdown-sweep.sh scales the worker to zero with no drain. That is what
# enrich_passage_context did every summer night, starting at 21:45 UTC with a
# 3 h budget against a 00:00 UTC (PDT) stop, and nothing here noticed,
# because only the start was compared.

#: hatchet_sdk's default execution_timeout when a task sets none.
HATCHET_DEFAULT_EXECUTION_TIMEOUT_MINUTES = 1.0

_WORKFLOW_DECL = re.compile(
    r'(\w+)\s*=\s*hatchet\.workflow\(\s*name="([^"]+)"(.*?)\n\)', re.S,
)
_ON_CRONS_BLOCK = re.compile(r"on_crons\s*=\s*\[([^\]]*)\]", re.S)
_DURATION_STR = re.compile(r"^(\d+)([smhd])$")
_DURATION_TIMEDELTA = re.compile(
    r"timedelta\(\s*(hours|minutes|seconds)\s*=\s*(\d+)\s*\)",
)
_UNIT_MINUTES = {"s": 1 / 60, "m": 1.0, "h": 60.0, "d": 1440.0}


def _timeout_minutes(decorator_args: str) -> float:
    """execution_timeout of one task decorator, in minutes."""
    string_form = re.search(r'execution_timeout\s*=\s*"([^"]+)"', decorator_args)
    if string_form:
        match = _DURATION_STR.match(string_form.group(1).strip())
        assert match, f"unparseable execution_timeout {string_form.group(1)!r}"
        return int(match.group(1)) * _UNIT_MINUTES[match.group(2)]
    delta = _DURATION_TIMEDELTA.search(decorator_args)
    if delta:
        per = {"hours": 60.0, "minutes": 1.0, "seconds": 1 / 60}[delta.group(1)]
        return int(delta.group(2)) * per
    assert "execution_timeout" not in decorator_args, (
        f"execution_timeout present but not parsed: {decorator_args!r}"
    )
    return HATCHET_DEFAULT_EXECUTION_TIMEOUT_MINUTES


def cron_budgets() -> list[tuple[str, str, str, list[int], float]]:
    """(module, workflow, cron, start minutes UTC, execution budget minutes).

    Fixed-hour crons only, one entry per expression, every hour of a comma
    list. The budget is the SUM of the workflow's task execution_timeouts
    (on_failure excluded): an upper bound for a DAG, exact for the
    single-task crons that make up nearly all of these.
    """
    found: list[tuple[str, str, str, list[int], float]] = []
    for path in sorted(WORKFLOWS.glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for decl in _WORKFLOW_DECL.finditer(text):
            var, name, body = decl.group(1), decl.group(2), decl.group(3)
            crons = [
                expression
                for block in _ON_CRONS_BLOCK.findall(body)
                for expression in re.findall(r'["\']([^"\']+)["\']', block)
                if len(expression.split()) == 5
            ]
            if not crons:
                continue
            tasks = re.findall(
                rf"@{re.escape(var)}\.(?:task|durable_task)\((.*?)\)\s*\n"
                r"\s*(?:async\s+)?def\b",
                text, re.S,
            )
            assert tasks, f"{path.name}: workflow {name} has crons but no task found"
            budget = sum(_timeout_minutes(args) for args in tasks)
            for expression in crons:
                minute, hour = expression.split()[:2]
                if hour == "*" or hour.startswith("*/"):
                    continue
                minute_value = (
                    0 if minute.startswith("*") else int(minute.split(",")[0])
                )
                starts = [int(h) * 60 + minute_value for h in hour.split(",")]
                found.append((path.name, name, expression, starts, budget))
    return found


def test_cron_budgets_are_readable() -> None:
    """Guards the guard: the end-time scan below found the crons it checks."""
    budgets = cron_budgets()
    assert len(budgets) >= 10, f"only {len(budgets)} cron budgets parsed: {budgets}"
    names = {name for _, name, _, _, _ in budgets}
    assert "enrich_passage_context" in names


def test_no_daily_cron_runs_past_the_shutdown() -> None:
    """start + execution_timeout must end before the NEXT stop, PDT and PST.

    ``shutdown_window()``'s first element is the earliest the stop lands in
    UTC across both DST halves (00:00 UTC in PDT), so ending by then is
    ending by the stop in both. schedule_timeout (queue wait) is not added:
    a run still queued at the stop never starts, so it wastes nothing.
    """
    stop, usable = shutdown_window()
    offenders = []
    for module, name, expression, starts, budget in cron_budgets():
        if module in EXEMPT:
            continue
        for start in starts:
            if stop <= start < usable:
                continue  # a start inside the window is the test above's job
            deadline = stop + 1440 if start >= usable else stop
            end = start + budget
            if end > deadline:
                offenders.append(
                    f"  {name:32s} {expression:16s} starts {_hhmm(start)} UTC, "
                    f"budget {budget:.0f} min, ends {_hhmm(int(end) % 1440)} "
                    f"UTC; the stop is {_hhmm(deadline % 1440)} UTC"
                )
    assert not offenders, (
        "These crons start in the open window but their execution budget "
        "runs into the nightly stop, where shutdown-sweep.sh scales the "
        "worker to zero with no drain and the run is killed mid-batch:\n"
        + "\n".join(offenders)
        + "\n\nMove the cron earlier or cap its execution_timeout so start + "
          "budget ends by the stop."
    )


def test_the_end_time_check_catches_the_enrich_regression() -> None:
    """The case HAT-14 found: 21:45 UTC plus a 3 h budget."""
    stop, usable = shutdown_window()
    start = 21 * 60 + 45
    assert usable <= start
    assert start + 180 > stop + 1440, "a 3 h budget from 21:45 must be caught"
    assert start + 120 <= stop + 1440, "the 2 h cap must clear the stop"


# ---------------------------------------------------------------------------
# pg_cron jobs inside RDS (AW-10, 2026-10-10)
# ---------------------------------------------------------------------------
# Hatchet is not the only scheduler that stops with the platform. bootstrap.sql
# schedules partman's maintenance with pg_cron INSIDE the RDS instance, and the
# instance is stopped for the same fifteen and a half hours. pg_cron does not
# run a job it missed, so `0 3 * * *` -- 03:00 UTC, closed in both DST halves --
# did not run late: it never ran, and nothing ever created or dropped a
# partition through it. Every check above reads the Hatchet workflows, so none
# of them could see it.

BOOTSTRAP_SQL = REPO / "deploy" / "aws" / "bootstrap.sql"
DATA_TF = REPO / "deploy" / "aws" / "terraform" / "data.tf"

#: `cron.schedule('<name>', '<expression>', ...)`, and the _in_database form.
_PG_CRON_CALL = re.compile(
    r"cron\.schedule(?:_in_database)?\(\s*'([^']+)'\s*,\s*'([^']+)'", re.S,
)


def _without_sql_comments(sql: str) -> str:
    """Drop `--` comments. bootstrap.sql carries the operator's copy-paste SQL
    for an already-bootstrapped database in its comments, and a commented-out
    cron.schedule() is not a job. (Naive on purpose: no job's command text
    contains `--`.)"""
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def pg_cron_daily_jobs(sql: str | None = None) -> list[tuple[str, str, int, int]]:
    """(job name, expression, hour, minute) UTC for each fixed-hour pg_cron job.

    Reads bootstrap.sql unless given SQL text. A job that fires many times a
    day (``*/10 * * * *``) is exempt for the reason the Hatchet scan exempts
    it, and so is pg_cron's ``30 seconds`` form. A fixed-hour expression this
    cannot read as plain integers (a range, a step inside a list) FAILS rather
    than being skipped: a job nobody can classify is exactly how this went
    unseen.
    """
    text = _without_sql_comments(
        BOOTSTRAP_SQL.read_text(encoding="utf-8") if sql is None else sql
    )
    found: list[tuple[str, str, int, int]] = []
    for name, expression in _PG_CRON_CALL.findall(text):
        fields = expression.split()
        if len(fields) != 5:
            continue
        minute, hour = fields[0], fields[1]
        if hour == "*" or hour.startswith("*/"):
            continue
        try:
            hours = [int(h) for h in hour.split(",")]
            minutes = [0] if minute.startswith("*") else [int(m) for m in minute.split(",")]
        except ValueError:
            raise AssertionError(
                f"pg_cron job {name!r} has the expression {expression!r}, which "
                "this test cannot place on the clock. Write the hour and minute "
                "as plain numbers (or a comma list) so it can be checked against "
                "the shutdown window."
            ) from None
        found.extend((name, expression, h, m) for h in hours for m in minutes)
    return found


def test_bootstrap_sql_schedules_partman_maintenance() -> None:
    """Guards the guard: if this stops parsing, the window check below passes
    vacuously over an empty list."""
    names = [name for name, *_ in pg_cron_daily_jobs()]
    assert "partman-maintenance" in names, (
        f"bootstrap.sql schedules {names or 'no fixed-hour pg_cron jobs'}; the "
        "partman-maintenance job is the one this section exists to watch. If it "
        "moved or was renamed, update the parser rather than letting this "
        "section check nothing."
    )


def test_no_pg_cron_job_fires_while_rds_is_stopped() -> None:
    stop, usable = shutdown_window()

    offenders = [
        (name, expression, f"{hour:02d}:{minute:02d} UTC")
        for name, expression, hour, minute in pg_cron_daily_jobs()
        if stop <= hour * 60 + minute < usable
    ]

    assert not offenders, (
        f"These pg_cron jobs fire between {_hhmm(stop)} and {_hhmm(usable)} "
        "UTC, when shutdown-sweep.sh has stopped the RDS instance pg_cron lives "
        "in, or when the startup sweep is still bringing it back. pg_cron does "
        "not run a job it missed; it does not run:\n"
        + "\n".join(
            f"  {name:24s} {expression:16s} {when}"
            for name, expression, when in sorted(offenders)
        )
        + f"\n\nMove them to {_hhmm(usable)} UTC or later, and put the SQL "
          "an operator runs on an already-bootstrapped database next to the "
          "change in bootstrap.sql: that file is applied once, by hand."
    )


def test_pg_cron_still_schedules_in_gmt() -> None:
    """The expressions above are read as UTC because pg_cron's default zone is
    GMT. Setting cron.timezone in the parameter group would move every job by
    the zone's offset without touching bootstrap.sql, and the check above would
    keep passing."""
    text = "\n".join(
        line for line in DATA_TF.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "cron.timezone" not in text, (
        "data.tf sets cron.timezone. The pg_cron expressions in bootstrap.sql "
        "are checked here as UTC; either drop the parameter or teach "
        "pg_cron_daily_jobs() the zone."
    )


def test_the_pg_cron_check_catches_the_03_00_regression() -> None:
    """The case AW-10 found, and the slot it moved to."""
    stop, usable = shutdown_window()
    job = "SELECT cron.schedule('partman-maintenance', '{}', $$CALL partman.run_maintenance_proc()$$);"

    (_, _, hour, minute), = pg_cron_daily_jobs(job.format("0 3 * * *"))
    assert stop <= hour * 60 + minute < usable, "03:00 UTC must be caught"

    (_, _, hour, minute), = pg_cron_daily_jobs(job.format("45 18 * * *"))
    assert not stop <= hour * 60 + minute < usable, "18:45 UTC must clear it"


def test_the_pg_cron_scan_reads_only_live_statements() -> None:
    sql = (
        "-- SELECT cron.schedule('old', '0 3 * * *', $$SELECT 1$$);\n"
        "--   SELECT cron.schedule('also-old', '0 4 * * *', $$SELECT 1$$);\n"
        "SELECT cron.unschedule(jobid) FROM cron.job WHERE jobname = 'x';\n"
        "SELECT cron.schedule(\n"
        "    'live',\n"
        "    '10,40 19 * * *',\n"
        "    $$SELECT 1$$\n"
        ");\n"
        "SELECT cron.schedule('often', '*/10 * * * *', $$SELECT 1$$);\n"
        "SELECT cron.schedule('seconds', '30 seconds', $$SELECT 1$$);\n"
    )
    assert pg_cron_daily_jobs(sql) == [
        ("live", "10,40 19 * * *", 19, 10),
        ("live", "10,40 19 * * *", 19, 40),
    ]


def test_an_unreadable_pg_cron_hour_fails_instead_of_being_skipped() -> None:
    with pytest.raises(AssertionError, match="cannot place"):
        pg_cron_daily_jobs("SELECT cron.schedule('range', '0 1-4 * * *', $$SELECT 1$$);")


# ---------------------------------------------------------------------------
# The dead-air suppressor against the longest night (AW-11, 2026-10-10)
# ---------------------------------------------------------------------------
# alerts.tf suppresses octane-dead-air for `period` after the shutdown sweep's
# completion marker. The schedule says the night is 15h30m. In a zone that
# changes its clocks the clock disagrees twice a year, 16h30m the night it
# falls back and 14h30m the night it springs forward, and a period sized for
# the schedule lets go before the startup sweep fires on the long night and
# pages about five minutes before the platform has been asked to start.
#
# America/Vancouver no longer has that night: British Columbia's 2026-03-08
# spring forward was its last clock change (tz database 2026b), so every night
# since is the schedule's length and scheduler.tf's slack is 0. The zone's
# real rules are used here, not a hardcoded figure, so the test notices if the
# zone, the crons, the slack or the rules change. That makes it only as good
# as the host's tz data, which _current_zone() checks before using it.

SCHEDULER_TF = REPO / "deploy" / "aws" / "terraform" / "scheduler.tf"
ALERTS_TF = REPO / "deploy" / "aws" / "terraform" / "alerts.tf"

#: The years whose nights the suppressor has to cover.
FUTURE_YEARS = range(2026, 2036)

#: Years in which America/Vancouver still changed its clocks twice: settled
#: history in every tz database release, so the reader is checked against them.
DST_YEARS = range(2020, 2026)


def _maintenance_timezone() -> str:
    text = TERRAFORM.read_text(encoding="utf-8")
    block = re.search(r'variable\s+"maintenance_timezone"\s*\{(.*?)\n\}', text, re.S)
    assert block, "no maintenance_timezone variable"
    match = re.search(r'default\s*=\s*"([^"]+)"', block.group(1))
    assert match, "maintenance_timezone has no default"
    return match.group(1)


def _current_zone(tz_name: str) -> ZoneInfo:
    """The zone, from tz data new enough to know 2026's rules, or a failure.

    zoneinfo reads the host's tz database before the tzdata package, and a
    host's copy can be years old. One that predates 2026b still has
    America/Vancouver falling back on 2026-11-01, so it measures a 16h30m
    night that no longer happens and sends whoever reads the failure off to
    add an hour to every morning's page. January 2027 is -08 there in every
    release before 2026b and -07 in every release since.
    """
    offset = datetime(2027, 1, 15, 12, tzinfo=ZoneInfo("America/Vancouver")).utcoffset()
    hours = (offset or timedelta()).total_seconds() / 3600
    assert offset == timedelta(hours=-7), (
        f"this host's tz database predates 2026b: it has America/Vancouver at "
        f"UTC{hours:+g} in January 2027, where British Columbia has stayed at "
        "UTC-7 since its 2026-03-08 spring forward, so the night lengths it "
        "gives are wrong. Update the host's tzdata, or run with "
        "`PYTHONTZPATH= uv run --with tzdata pytest ...` to read the tzdata "
        "package instead."
    )
    return ZoneInfo(tz_name)


def night_lengths(tz: ZoneInfo, years: range) -> list[int]:
    """Elapsed minutes from each day's shutdown fire to the next startup fire.

    Both are local-time schedules (EventBridge Scheduler takes a timezone), so
    the instants come from the tz database, and the subtraction is done in UTC:
    two aware datetimes sharing a tzinfo subtract as wall-clock time, which
    would report every night as 15h30m and hide exactly what this measures.
    """
    stop_h, stop_m = divmod(_local_minutes("shutdown_cron"), 60)
    start_h, start_m = divmod(_local_minutes("startup_cron"), 60)
    same_day = (start_h, start_m) > (stop_h, stop_m)

    nights: list[int] = []
    day = date(years.start, 1, 1)
    while day.year < years.stop:
        stop_at = datetime(day.year, day.month, day.day, stop_h, stop_m, tzinfo=tz)
        start_day = day if same_day else day + timedelta(days=1)
        start_at = datetime(
            start_day.year, start_day.month, start_day.day, start_h, start_m, tzinfo=tz
        )
        elapsed = start_at.astimezone(UTC) - stop_at.astimezone(UTC)
        nights.append(int(elapsed.total_seconds() // 60))
        day += timedelta(days=1)
    return nights


def _scheduler_local_minutes(name: str, text: str) -> int:
    """A scheduler.tf local that is an integer, or a sum of integers and locals.

    `maintenance_window_minutes` is derived from the two crons, as Terraform
    derives it. Anything fancier than a sum is refused rather than guessed at,
    so the day it becomes something this cannot read, this fails.
    """
    if name == "maintenance_window_minutes":
        return (_local_minutes("startup_cron") - _local_minutes("shutdown_cron")) % 1440
    match = re.search(rf"^  {name}\s*=\s*([^\n#]+)", text, re.M)
    assert match, f"scheduler.tf defines no local {name!r}"
    total = 0
    for term in (t.strip() for t in match.group(1).split("+")):
        if term.isdigit():
            total += int(term)
            continue
        ref = re.fullmatch(r"local\.(\w+)", term)
        assert ref, (
            f"cannot evaluate {term!r} in local {name!r}: keep it a sum of "
            "integers and other locals, or teach this test the new expression"
        )
        total += _scheduler_local_minutes(ref.group(1), text)
    return total


def suppressor_minutes() -> int:
    """The period, in minutes, of alerts.tf's maintenance_window alarm."""
    block = re.search(
        r'resource\s+"aws_cloudwatch_metric_alarm"\s+"maintenance_window"\s*\{(.*?)\n\}',
        ALERTS_TF.read_text(encoding="utf-8"), re.S,
    )
    assert block, "no maintenance_window alarm in alerts.tf"
    period = re.search(r"^\s*period\s*=\s*local\.(\w+)\s*\*\s*60\s*$", block.group(1), re.M)
    assert period, (
        "the maintenance_window alarm's period is no longer `local.<name> * 60`; "
        "teach suppressor_minutes() the new shape"
    )
    return _scheduler_local_minutes(period.group(1), SCHEDULER_TF.read_text(encoding="utf-8"))


def test_the_longest_night_is_what_the_clock_says_not_what_the_schedule_says() -> None:
    """Guards the guard, on years whose answer is settled: Vancouver still
    changed its clocks in 2020-2025, so six nights an hour long and six an
    hour short. A reader that subtracted wall-clock times would see the
    schedule's length every night, and would pass a suppressor sized for the
    schedule in a zone that still falls back."""
    nights = night_lengths(ZoneInfo("America/Vancouver"), DST_YEARS)
    nominal = (_local_minutes("startup_cron") - _local_minutes("shutdown_cron")) % 1440

    assert sorted(set(nights)) == [nominal - 60, nominal, nominal + 60], (
        f"nights in 2020-2025 ran {sorted(set(nights))} min against a scheduled "
        f"{nominal}; the reader no longer sees the clock changes"
    )
    assert nights.count(nominal + 60) == 6 and nights.count(nominal - 60) == 6


def test_the_suppressor_covers_the_longest_night_of_the_year() -> None:
    longest = max(night_lengths(_current_zone(_maintenance_timezone()), FUTURE_YEARS))
    period = suppressor_minutes()

    assert period >= longest, (
        f"the maintenance_window alarm suppresses for {period} min after the "
        f"shutdown-complete marker, but the longest night runs {longest} min "
        "from the shutdown fire to the startup fire. The suppressor lets go "
        "before the startup sweep has fired, and octane-dead-air emails about "
        "five minutes before the platform is asked to start. Raise "
        "local.dst_slack_minutes in scheduler.tf; the extension_period in "
        "alerts.tf covers the sweep's own runtime and is not where this belongs."
    )


def test_a_period_equal_to_the_schedule_would_not_cover_a_night_the_clocks_fall_back() -> None:
    """The regression the suppressor test exists for, shown on a zone that
    still had the night: Vancouver before 2026, whose night of 2025-11-01 was
    an hour longer than the schedule. It is also where scheduler.tf's "set 60
    for a zone that still observes DST" comes from."""
    nominal = (_local_minutes("startup_cron") - _local_minutes("shutdown_cron")) % 1440
    longest = max(night_lengths(ZoneInfo("America/Vancouver"), DST_YEARS))
    assert longest - nominal == 60


def test_the_slack_is_only_the_clock_change_and_not_more() -> None:
    """The price of the slack is paid every morning (the page for a platform
    that never came up is late by exactly the slack), so it is what the
    zone's clock changes add to the longest night, and 0 in a zone that has
    none left."""
    nominal = (_local_minutes("startup_cron") - _local_minutes("shutdown_cron")) % 1440
    longest = max(night_lengths(_current_zone(_maintenance_timezone()), FUTURE_YEARS))
    assert suppressor_minutes() <= longest, (
        f"the suppressor is {suppressor_minutes()} min against a longest night "
        f"of {longest} (schedule {nominal}). Every minute above the longest "
        "night delays the morning page for a platform that did not come up; "
        "lower local.dst_slack_minutes in scheduler.tf."
    )


def test_the_scheduler_local_reader_follows_sums_and_refuses_anything_else() -> None:
    text = "  a = 60\n  b = local.maintenance_window_minutes + local.a\n  c = local.a * 2\n"
    window = (_local_minutes("startup_cron") - _local_minutes("shutdown_cron")) % 1440
    assert _scheduler_local_minutes("a", text) == 60
    assert _scheduler_local_minutes("b", text) == window + 60
    with pytest.raises(AssertionError, match="cannot evaluate"):
        _scheduler_local_minutes("c", text)
    with pytest.raises(AssertionError, match="defines no local"):
        _scheduler_local_minutes("missing", text)
