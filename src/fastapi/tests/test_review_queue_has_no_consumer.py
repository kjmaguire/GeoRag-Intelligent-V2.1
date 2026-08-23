"""`silver.review_queue` is written and never read.

ingest_pdf INSERTs a row for every page whose OCR confidence was too low
to index. Nothing anywhere moves that row's lifecycle off 'pending':
there is no disposition endpoint, no re-OCR consumer, and no page. The
`IngestionReviewDispositionChanged` event and the
`private-admin.ingestion-review` channel that would carry it both exist
and have no dispatcher and no subscriber.

WHY THIS IS A TEST AND NOT A FIX
    Building the triage surface is a feature -- a list, a disposition
    endpoint, a re-OCR dispatch, and a decision about who is allowed to
    accept a page into the corpus. What could be fixed now was the UI
    lying about it: the Reports quality strip labelled the count "AWAITING
    OCR / Tier-2 pipeline" and the flagged count "needs review", naming a
    pipeline that does not run and a review nobody can perform. A rising
    number read as work in progress when it measured how much of the
    corpus chat cannot see.

    That copy is now accurate, and accurate copy is exactly the kind of
    thing that goes stale silently the moment someone builds the consumer.
    This test fails then, and names the file to update.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FASTAPI_APP = REPO / "src" / "fastapi" / "app"

#: The one legitimate writer.
WRITER = "src/fastapi/app/hatchet_workflows/ingest_pdf.py"

#: UI copy that is only true while the queue has no consumer.
UI_COPY = REPO / "resources/js/Pages/Foundry/Reports.tsx"

#: A write that moves an existing row's lifecycle forward. The INSERT in
#: ingest_pdf is not one of these.
MUTATION = re.compile(
    r"UPDATE\s+silver\.review_queue|DELETE\s+FROM\s+silver\.review_queue",
    re.IGNORECASE,
)


def _python_sources() -> list[Path]:
    return sorted(p for p in FASTAPI_APP.rglob("*.py") if "__pycache__" not in p.parts)


def _without_comments(source: str) -> str:
    """Strip JS/JSX comments before searching for a forbidden phrase.

    Necessary, not fastidious. The fix that removed "Tier-2 pipeline" from
    the UI left a JSX comment explaining what the label used to claim and
    why it was wrong — house style, and worth keeping — so a naive search
    of the raw file finds the phrase inside its own obituary and reports
    the fix as not landed. This is the sixth time in this audit that a
    source-scanning check has matched the comment describing the thing it
    was written to forbid.

    `://` is spared so a URL's scheme is not mistaken for a line comment.
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"(?<!:)//[^\n]*", "", source)


def test_nothing_advances_a_review_queue_row() -> None:
    movers = [
        p.relative_to(REPO).as_posix()
        for p in _python_sources()
        if MUTATION.search(p.read_text(encoding="utf-8", errors="replace"))
    ]

    assert movers == [], (
        "something now moves silver.review_queue rows: "
        + ", ".join(movers)
        + " -- the queue has a consumer, so the Reports quality strip's "
        f"'no triage queue yet' copy in {UI_COPY.relative_to(REPO).as_posix()} "
        "is stale. Update both."
    )


def test_the_writer_is_still_where_this_file_says_it_is() -> None:
    """A pointer to a file that moved is worse than no pointer."""
    assert (REPO / WRITER).is_file()
    assert "silver.review_queue" in (REPO / WRITER).read_text(
        encoding="utf-8", errors="replace",
    )


def test_the_ui_does_not_promise_a_pipeline() -> None:
    """The specific phrases that were wrong, kept out by name.

    Both named something the system does not do: 'Tier-2 pipeline' a
    process that never runs, 'needs review' an action with nowhere to
    perform it.
    """
    copy = _without_comments(UI_COPY.read_text(encoding="utf-8", errors="replace"))

    for phrase in ("Tier-2 pipeline", "'needs review'"):
        assert phrase not in copy, (
            f"{UI_COPY.name} says {phrase!r} again. Nothing drains "
            "silver.review_queue, so that label points a geologist at a "
            "queue and a process that do not exist. If one has been built, "
            "delete this assertion along with the claim."
        )


def test_the_honest_copy_is_present() -> None:
    """Guards the guard: the assertion above passes trivially if the whole
    quality strip is deleted or renamed."""
    copy = _without_comments(UI_COPY.read_text(encoding="utf-8", errors="replace"))

    assert "AWAITING OCR" in copy, "the quality strip moved; re-point this test"
    assert "no triage queue yet" in copy, (
        "the AWAITING OCR stat lost its explanation. A bare growing integer "
        "with no sub-label reads as a backlog being worked."
    )
