"""`silver.data_quality_flags` is read by the UI and written by nothing live.

WHAT THIS IS
    Not a Phase 3 checklist item -- found on 2026-08-21 while labelling
    `silver_dq_flag_writer.py` NOT WIRED, and it is the THIRD instance of
    one pattern in this codebase:

        SourcesController   2026-08-17  joined bronze.provenance for
                                        document_passages rows that the
                                        live ingest path never writes
        ReportController    2026-08-18  reverse-looked-up passages through
                                        two bronze.provenance rows, same
                                        cause
        this one            2026-08-21  DrillholeDetailController and
                                        ReportController read
                                        silver.data_quality_flags

    The only writer is ``src/dagster/georag_dagster/dq_writer.py``. Dagster
    went dormant 2026-07-28 and has no container app in the Azure resource
    group, so the table is empty in production and both surfaces render
    against nothing.

WHY IT WAS WORSE THAN AN EMPTY PANEL
    ``DataQualityFlagsBadge`` returned null when ``open_total === 0``, with
    the comment "No flags -> no UI noise ... the well-behaved-collar happy
    path". That is the right design IF flags are being computed. They were
    not, so the absence of a badge meant "nobody has checked this hole"
    while it read as "this hole is clean".

    For a geologist deciding whether to trust an interval, those are
    opposite statements. Silence that means "no problems found" and silence
    that means "no rule has run" cannot share a rendering.

    CLOSED 2026-08-22 in the UI pass. ``dataQualityFlagSummary`` now returns
    an ``evaluated`` flag -- has any rule ever produced a finding in this
    project -- and the badge renders a muted "not checked" chip rather than
    nothing when it is false. The two silences are now two renderings.

WHY THIS FILE IS STILL A TEST AND NOT A FIX
    The UI no longer over-claims, but the table is still empty: the five
    rule families that would fill it do not exist, and the FastAPI-side
    writer has no callers. What a test CAN do is stop the annotations from
    outliving the problem: when a live writer appears, this fails and names
    the places that say the table is empty -- including the `evaluated`
    comment, which will then be describing a live signal rather than a
    permanently-false one.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FASTAPI_APP = REPO / "src" / "fastapi" / "app"
LARAVEL_APP = REPO / "app"

#: Files that currently state, in a comment, that this table has no live
#: writer. When the assertion below flips, every one of these is wrong.
ANNOTATED = (
    "src/fastapi/app/services/silver_dq_flag_writer.py",
    "app/Http/Controllers/Foundry/DrillholeDetailController.php",
    "app/Http/Controllers/Foundry/ReportController.php",
)

_WRITE = ("INSERT INTO silver.data_quality_flags",
          "UPDATE silver.data_quality_flags")


def _live_writers() -> list[str]:
    """Files under the DEPLOYED trees that write the table.

    src/dagster is excluded on purpose: it is the one place a writer does
    exist, and it is dormant by explicit decision (see src/dagster/
    DORMANT.md). Its presence is the whole point of this file.
    """
    found = []
    for root in (FASTAPI_APP, LARAVEL_APP):
        for path in root.rglob("*.p*"):
            if path.suffix not in {".py", ".php"}:
                continue
            if path.name == "silver_dq_flag_writer.py":
                continue  # complete, callable, and called by nothing
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(statement in text for statement in _WRITE):
                found.append(str(path.relative_to(REPO)).replace("\\", "/"))
    return sorted(found)


def test_nothing_deployed_writes_data_quality_flags() -> None:
    writers = _live_writers()

    assert writers == [], (
        "silver.data_quality_flags now has a live writer:\n"
        + "\n".join(f"  - {w}" for w in writers)
        + "\n\nGood news, and it makes three annotations stale. Remove the "
          "'no live writer' notes from:\n"
        + "\n".join(f"  - {f}" for f in ANNOTATED)
        + "\n\nand revisit DataQualityFlagsBadge, which returns null on "
          "open_total === 0 -- once rules really run, that silence means "
          "'clean' again and the empty state should say so."
    )


def test_the_helper_that_would_write_it_is_still_uncalled() -> None:
    """The FastAPI-side helper is complete. Only its callers are missing."""
    helper = FASTAPI_APP / "services" / "silver_dq_flag_writer.py"
    assert helper.is_file(), "the helper was deleted; drop this file too"

    callers = [
        str(path.relative_to(FASTAPI_APP)).replace("\\", "/")
        for path in FASTAPI_APP.rglob("*.py")
        if path.name != "silver_dq_flag_writer.py"
        and "silver_dq_flag_writer" in path.read_text(
            encoding="utf-8", errors="replace")
    ]

    assert callers == [], (
        "silver_dq_flag_writer now has callers: " + ", ".join(callers)
        + " -- update its NOT WIRED banner and this test."
    )


def test_the_annotations_this_file_points_at_still_exist() -> None:
    """A pointer to a file that moved is worse than no pointer."""
    missing = [f for f in ANNOTATED if not (REPO / f).is_file()]
    assert missing == [], f"annotated files have moved or gone: {missing}"


def test_each_annotated_file_actually_carries_the_note() -> None:
    """Otherwise a reformat quietly drops the warning and this file keeps
    claiming it is there.

    Whitespace is collapsed before matching: these are comment blocks, so
    the phrase legitimately wraps across a line, and a check that cannot
    see a "NO LIVE" / "WRITER" line break fails on formatting rather than
    on substance -- which is exactly the false alarm that gets a guard
    deleted.
    """
    import re as _re

    silent = [
        f for f in ANNOTATED
        if "no live writer" not in _re.sub(
            r"\s+", " ",
            (REPO / f).read_text(encoding="utf-8", errors="replace").lower(),
        )
    ]
    assert silent == [], (
        "these files are listed as annotated but no longer say so: "
        + ", ".join(silent)
    )
