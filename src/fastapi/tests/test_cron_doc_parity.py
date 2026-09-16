"""The written-down schedule has to be the schedule that runs.

WHY THIS EXISTS
    On 2026-09-16 twenty Hatchet crons moved, to fit inside an eight-hour
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

    Both directions are deliberate. (1) catches a module lying about
    itself, which is what the reader in the file sees. (2) catches the doc
    drifting from the code, which is what the reader at the architecture
    level sees.

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
