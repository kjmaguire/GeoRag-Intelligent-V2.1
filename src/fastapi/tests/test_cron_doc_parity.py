"""The written-down schedule has to be the schedule that runs.

WHY THIS EXISTS
    On 2026-09-16 twenty Hatchet crons moved, to fit inside a business-day
    open window. The ``on_crons=`` values moved. Roughly thirty places that
    quoted those values did not: ``worker.py``'s registration comments,
    ``phase0_agents.py``'s header table and every one of its section
    banners, four module docstrings, an operator-facing log line in
    ``ingest_pdf.py``, and the whole cron table in the maintained manual at
    ``docs/architecture/manual/07-orchestration.md``.

    None of that breaks anything at runtime, which is exactly the problem.
    The next person to move a cron reads the stagger out of a comment, and
    the comment is describing a schedule that has not existed for a month.
    ``test_crons_avoid_the_shutdown_window.py`` catches a cron that CANNOT
    RUN; this catches a cron that runs at a time nobody wrote down.

WHAT IT CHECKS
    1. Every 5-field cron expression quoted in a ``Schedule:``/``Cron:``
       line of a workflow module is one that module actually declares.
    2. Every row of the manual's workflow tables that names a workflow and
       gives a cron agrees with that workflow's ``on_crons``.
    3. Every row of the manual's consolidated UTC timetable that gives a
       clock time and names workflows agrees with those workflows'
       ``on_crons``.
    4. The manual's EventBridge schedule table quotes the sweep crons and
       timezone that ``variables.tf`` actually declares. That table decides
       when EVERY cron above can run, so a stale row there invalidates the
       whole chapter rather than one line of it.

    All three directions are deliberate. (1) catches a module lying about
    itself, which is what the reader in the file sees. (2) catches the doc
    drifting from the code, which is what the reader at the architecture
    level sees. (3) exists because (1) and (2) both missed an entire stale
    table: when the crons moved on 2026-09-16 the §2.2 registry tables were
    re-pointed and §4's timetable was not, so the chapter carried the old
    schedule and the new one side by side for a day. The timetable's rows
    are keyed on a TIME rather than a workflow name, so the parser for (2)
    skipped every one of them.

WHAT IT DOES NOT CHECK
    Prose times ("nightly 17:00 UTC") are not parsed -- too many of them
    are correctly-dated history, and a regex cannot tell "it was 02:00
    until September" from "it is 02:00". Those were swept by hand. If this
    rots again it will rot in the prose first.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = Path(__file__).resolve().parent.parent / "app" / "hatchet_workflows"
MANUAL = REPO / "docs" / "architecture" / "manual" / "07-orchestration.md"
TERRAFORM = REPO / "deploy" / "aws" / "terraform" / "variables.tf"

#: `name="x"` ... `on_crons=[...]` inside one hatchet.workflow(...) call.
_WORKFLOW = re.compile(r'hatchet\.workflow\(\s*name="([^"]+)"(.*?)\n\)', re.S)
_ON_CRONS = re.compile(r"on_crons\s*=\s*\[([^\]]*)\]", re.S)
_QUOTED = re.compile(r'["\']([^"\']+)["\']')
#: A cron in running text or a markdown table, always inside backticks.
_BACKTICKED = re.compile(r"`([0-9*/,\-]+(?: +[0-9*/,\-]+){4})`")


def _is_cron(text: str) -> bool:
    return len(text.split()) == 5


def declared_crons() -> dict[str, set[str]]:
    """Workflow name -> the cron expressions it declares. The ground truth."""
    found: dict[str, set[str]] = {}
    for path in sorted(WORKFLOWS.glob("*.py")):
        for match in _WORKFLOW.finditer(path.read_text(encoding="utf-8")):
            crons = {
                expression
                for block in _ON_CRONS.findall(match.group(2))
                for expression in _QUOTED.findall(block)
                if _is_cron(expression)
            }
            if crons:
                found[match.group(1)] = crons
    return found


def crons_by_module() -> dict[str, set[str]]:
    """Module filename -> every cron any workflow in it declares."""
    found: dict[str, set[str]] = {}
    for path in sorted(WORKFLOWS.glob("*.py")):
        crons = {
            expression
            for block in _ON_CRONS.findall(path.read_text(encoding="utf-8"))
            for expression in _QUOTED.findall(block)
            if _is_cron(expression)
        }
        if crons:
            found[path.name] = crons
    return found


def test_the_scan_found_the_workflows() -> None:
    """Guards the guard: an empty scan passes everything below vacuously."""
    declared = declared_crons()
    assert len(declared) >= 20, f"only {len(declared)} scheduled workflows: {declared}"
    assert "audit_ledger_verify" in declared


def test_no_module_quotes_a_schedule_it_does_not_declare() -> None:
    by_module = crons_by_module()
    offenders: list[str] = []

    for path in sorted(WORKFLOWS.glob("*.py")):
        actual = by_module.get(path.name)
        if not actual:
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if "on_crons" in line:
                continue
            if not re.search(r"\b(Schedule|Cron)s?\b\s*:", line):
                continue
            for quoted in _BACKTICKED.findall(line):
                if quoted not in actual:
                    offenders.append(
                        f"  {path.name}:{number} cites `{quoted}`, "
                        f"but the module declares {sorted(actual)}\n"
                        f"      {line.strip()}"
                    )

    assert not offenders, (
        "These modules document a schedule they do not run:\n"
        + "\n".join(offenders)
        + "\n\nThe cron moved and the docstring did not. Update the "
        "docstring -- and say what the slot is FOR, because the absolute "
        "hour has moved three times and the stagger is the part that "
        "carries meaning."
    )


def _manual_rows() -> list[tuple[int, str, set[str], set[str]]]:
    """(line number, raw line, workflow names, crons) per manual table row."""
    rows: list[tuple[int, str, set[str], set[str]]] = []
    for number, line in enumerate(MANUAL.read_text(encoding="utf-8").splitlines(), 1):
        if not line.startswith("| `"):
            continue
        cells = line.split("|")
        if len(cells) < 4:
            continue
        names = set(re.findall(r"`([a-z0-9_]+)`", cells[1]))
        crons = {c for c in _BACKTICKED.findall(cells[2]) if _is_cron(c)}
        if names and crons:
            rows.append((number, line, names, crons))
    return rows


def test_the_manual_table_was_found() -> None:
    rows = _manual_rows()
    assert len(rows) >= 15, f"only {len(rows)} cron rows parsed from {MANUAL.name}"


#: `| 17:00 |` or `| 17:00 Mon |`. Anything else in the first cell -- "every
#: 15 min", ":00 hourly", "15:30 (PDT) / 16:30 (PST)" -- is a cadence or a
#: sweep, not a workflow slot, and is skipped.
_TIMETABLE_CELL = re.compile(r"^\s*(\d{1,2}):(\d{2})(?:\s+\S+)?\s*$")


def _timetable_rows() -> list[tuple[int, str, int, set[str]]]:
    """(line number, raw line, minutes past UTC midnight, workflow names)."""
    rows: list[tuple[int, str, int, set[str]]] = []
    for number, line in enumerate(MANUAL.read_text(encoding="utf-8").splitlines(), 1):
        cells = line.split("|")
        if len(cells) < 4:
            continue
        when = _TIMETABLE_CELL.match(cells[1])
        if not when:
            continue
        names = set(re.findall(r"`([a-z0-9_]+)`", cells[2]))
        if names:
            rows.append(
                (number, line, int(when.group(1)) * 60 + int(when.group(2)), names)
            )
    return rows


def test_the_timetable_was_found() -> None:
    rows = _timetable_rows()
    assert len(rows) >= 12, f"only {len(rows)} timetable rows parsed from {MANUAL.name}"


def test_the_timetable_agrees_with_the_code() -> None:
    declared = declared_crons()
    offenders: list[str] = []

    for number, line, minutes, names in _timetable_rows():
        for name in sorted(names):
            actual = declared.get(name)
            if actual is None:
                continue  # GitHub Actions, or a workflow with no cron
            slots = set()
            for expression in actual:
                minute, hour = expression.split()[:2]
                if hour == "*" or hour.startswith("*/"):
                    continue  # a cadence, listed in the table's top rows
                slots.add(
                    int(hour.split(",")[0]) * 60
                    + (0 if minute.startswith("*") else int(minute.split(",")[0]))
                )
            if slots and minutes not in slots:
                offenders.append(
                    f"  {MANUAL.name}:{number} `{name}` is listed at "
                    f"{minutes // 60:02d}:{minutes % 60:02d} UTC, but runs at "
                    + ", ".join(f"{s // 60:02d}:{s % 60:02d}" for s in sorted(slots))
                    + f"\n      {line.strip()}"
                )

    assert not offenders, (
        "The manual's consolidated UTC timetable disagrees with the code:\n"
        + "\n".join(offenders)
        + "\n\nThis is the table an operator reads to find out what the "
        "platform does overnight. It went a day carrying the pre-2026-09-16 "
        "schedule while the rest of the chapter carried the new one; fix the "
        "table, not this test."
    )


#: `| `georag-shutdown` | `cron(0 17 * * ? *)` | `America/Vancouver` | ... |`
_SWEEP_ROW = re.compile(
    r"^\|\s*`georag-(shutdown|startup)`\s*\|\s*`([^`]+)`\s*\|\s*`([^`]+)`"
)


def _terraform_default(variable: str) -> str:
    text = TERRAFORM.read_text(encoding="utf-8")
    block = re.search(rf'variable\s+"{variable}"\s*\{{(.*?)\n\}}', text, re.S)
    assert block, f"no variable {variable!r} in {TERRAFORM.name}"
    match = re.search(r'default\s*=\s*"([^"]+)"', block.group(1))
    assert match, f"no default for {variable!r}"
    return match.group(1)


def test_the_manual_quotes_the_sweep_schedule_terraform_declares() -> None:
    """The stale row that hid all the others.

    §3.2's table and the paragraph under it carried `cron(0 23 * * ? *)` /
    `cron(0 6 * * ? *)` on `America/Los_Angeles` -- the pre-2026-09-16
    schedule -- while §2.2 of the same chapter already described the new one.
    Neither of the checks above could see it: the cells hold a sweep name and
    a 6-field EventBridge expression, not a workflow name and a 5-field
    Hatchet cron.
    """
    expected = {
        "shutdown": _terraform_default("shutdown_cron"),
        "startup": _terraform_default("startup_cron"),
    }
    timezone = _terraform_default("maintenance_timezone")

    seen: set[str] = set()
    offenders: list[str] = []

    for number, line in enumerate(MANUAL.read_text(encoding="utf-8").splitlines(), 1):
        row = _SWEEP_ROW.match(line)
        if not row:
            continue
        which, cron, zone = row.groups()
        seen.add(which)
        if cron != expected[which]:
            offenders.append(
                f"  {MANUAL.name}:{number} georag-{which} -> doc `{cron}`, "
                f"terraform `{expected[which]}`"
            )
        if zone != timezone:
            offenders.append(
                f"  {MANUAL.name}:{number} georag-{which} -> doc timezone "
                f"`{zone}`, terraform `{timezone}`"
            )

    assert seen == {"shutdown", "startup"}, (
        f"only found sweep rows for {sorted(seen)} in {MANUAL.name} — §3.2's "
        "EventBridge table is the thing this checks, and it has moved or been "
        "reformatted out from under this regex"
    )
    assert not offenders, (
        "The manual's EventBridge schedule table disagrees with the "
        "Terraform:\n" + "\n".join(offenders) + "\n\nThis table decides "
        "when every cron in the chapter can run at all. Fix the table."
    )


def test_the_manual_agrees_with_the_code() -> None:
    declared = declared_crons()
    offenders: list[str] = []

    for number, line, names, crons in _manual_rows():
        for name in sorted(names):
            actual = declared.get(name)
            if actual is None:
                continue  # not a scheduled workflow, or named in prose
            if actual != crons:
                offenders.append(
                    f"  {MANUAL.name}:{number} `{name}` -> doc {sorted(crons)}, "
                    f"code {sorted(actual)}\n      {line.strip()}"
                )

    assert not offenders, (
        "The manual's workflow table disagrees with the code:\n"
        + "\n".join(offenders)
        + f"\n\n{MANUAL.name} is the file-cited companion to the "
        "architecture doc -- it is what someone reads to find out when a "
        "thing runs. Fix the table, not this test."
    )
