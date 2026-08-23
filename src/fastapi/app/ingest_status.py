"""The vocabulary of ``silver.ingest_progress.status``.

A leaf module on purpose. The canonical definition used to live in
``app.hatchet_workflows._progress``, and importing that pulls in
``app.hatchet_workflows.__init__``, which constructs a ``Hatchet()`` client at
import time. Anything that only needs to know which statuses are terminal --
``app.services.laravel_bridge`` decides from it whether a callback is worth
retrying -- should not have to stand up a workflow-engine client to find out.

``_progress`` re-exports both names, so every existing
``_progress.TERMINAL_STATUSES`` / ``_progress.TERMINAL_STATUS_SQL`` reference
keeps working and there is still exactly one definition.

Note this is the vocabulary of ``silver.ingest_progress`` specifically.
``silver.archive_ingest_runs`` is a different table with a deliberately
different set (no ``timed_out``: nothing sweeps archive parents), declared in
``_archive_progress``. They are not copies of each other.
"""
from __future__ import annotations

#: Statuses a row can never leave.
#:
#: 'partial' belongs here and was missing for the first day of its
#: existence. ``mark_completed_by_run`` writes it for a run that reached the
#: end and wrote rows while also collecting warnings -- so the row is
#: finished, and every other guard in ``_progress`` read it as still
#: running. The 15-minute stale-run sweep therefore relabelled each
#: partial ingest "Timed out" while its rows sat in the database.
TERMINAL_STATUSES: tuple[str, ...] = (
    "completed",
    "partial",
    "failed",
    "cancelled",
    "timed_out",
)

#: The same tuple rendered as a SQL list.
#:
#: Seven conditional-update guards used to spell the set out by hand, which
#: is how 'partial' came to be terminal to one function and non-terminal to
#: the rest. Deriving the fragment means adding a status is one edit, and a
#: guard cannot disagree with the tuple. Every value is a module constant;
#: nothing here is interpolated from a caller.
TERMINAL_STATUS_SQL: str = ",".join(f"'{_s}'" for _s in TERMINAL_STATUSES)

__all__ = ["TERMINAL_STATUSES", "TERMINAL_STATUS_SQL"]
