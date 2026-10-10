"""Reading a column of drill dates without guessing day from month.

Audit finding 15 (2026-10). ``csv_collar._parse_date`` tried a fixed list of
formats and returned None for anything it could not read:

* an Excel date cell reaches the CSV parser as ``2023-04-05T00:00:00.000000``
  (the workbook is written to an in-memory CSV by Polars), which none of the
  formats read, so every ``drill_date`` of a workbook was NULL with nothing in
  the run to say so;
* ``03/04/2023`` matched ``%d/%m/%Y`` first and was stored as 3 April whether
  the file meant 3 April or March 4th - a plausible, silent default.

What this does instead:

* ISO dates and ISO date-times (``T`` or space, optional fraction and zone)
  are read as the date they name; the time of day is not a drill date's
  business.
* A ``d/m/Y``-style cell (``/``, ``.`` or ``-`` separated) that only one order
  can explain (``25/04/2023``) is read that way.
* One that BOTH orders explain and that mean different days (``03/04/2023``)
  is decided by the column: any unambiguous cell elsewhere in it fixes the
  convention for the file, and the run says which cell did. With no such cell
  the date is left EMPTY and reported as ``date_ambiguous`` with both readings
  - refusing is the honest outcome; the column's author can disambiguate it.
* Anything non-empty that is not a date at all is left empty and reported as
  ``date_unparseable`` with the rows and values.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

#: 2023-04-05, 2023-04-05T00:00:00, 2023-04-05 10:30:15.123456, ...Z / +02:00
_ISO = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?\s*(?:Z|[+-]\d{2}(?::?\d{2})?)?)?$",
)
#: 20230405
_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
#: 05/04/2023, 5.4.2023, 05-04-2023 - day and month in an order to be decided.
_NUMERIC = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})$")
_NAMED_MONTH_FORMATS = ("%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y")

#: Cells quoted per warning.
_QUOTED = 5
#: Rows kept in a warning's structured context.
_LISTED = 20


def _make(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


@dataclass
class DateReader:
    """Reads the date cells of ONE file and remembers what it could not read.

    Build it from the whole column (``DateReader(cells)``) so the convention
    is decided across every row before any row is judged, then call
    :meth:`read` per row and :meth:`warnings` once at the end.
    """

    cells: Iterable[Any] = ()
    first_row: int = 2
    #: ``"dmy"``, ``"mdy"``, ``"mixed"`` (both seen) or None (no evidence).
    convention: str | None = field(init=False, default=None)
    #: ``(row, text)`` of the first unambiguous cell for each order seen.
    evidence: dict[str, tuple[int, str]] = field(init=False, default_factory=dict)
    unparseable: list[tuple[int, str]] = field(init=False, default_factory=list)
    ambiguous: list[tuple[int, str, date, date]] = field(init=False, default_factory=list)
    #: Ambiguous cells the column's convention settled: ``(row, text)``.
    inferred: list[tuple[int, str]] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        seen: dict[str, tuple[int, str]] = {}
        for offset, cell in enumerate(self.cells):
            text = self._text(cell)
            match = _NUMERIC.match(text) if text else None
            if match is None:
                continue
            first, second, year = (int(g) for g in match.groups())
            day_first = _make(year, second, first)
            month_first = _make(year, first, second)
            row = self.first_row + offset
            if day_first and not month_first:
                seen.setdefault("dmy", (row, text))
            elif month_first and not day_first:
                seen.setdefault("mdy", (row, text))
        self.evidence = seen
        if len(seen) == 2:
            self.convention = "mixed"
        elif seen:
            self.convention = next(iter(seen))

    @staticmethod
    def _text(cell: Any) -> str:
        if cell is None or isinstance(cell, (date, datetime)):
            return ""
        return str(cell).strip()

    def read(self, row: int, value: Any) -> date | None:
        """The date *value* names, or None (reported when not simply empty)."""
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = self._text(value)
        if not text:
            return None

        iso = _ISO.match(text) or _COMPACT.match(text)
        if iso:
            result = _make(*(int(g) for g in iso.groups()))
            if result is not None:
                return result
            self.unparseable.append((row, text))
            return None

        numeric = _NUMERIC.match(text)
        if numeric:
            first, second, year = (int(g) for g in numeric.groups())
            day_first = _make(year, second, first)
            month_first = _make(year, first, second)
            if day_first and month_first:
                if day_first == month_first:
                    return day_first
                if self.convention == "dmy":
                    self.inferred.append((row, text))
                    return day_first
                if self.convention == "mdy":
                    self.inferred.append((row, text))
                    return month_first
                self.ambiguous.append((row, text, day_first, month_first))
                return None
            if day_first or month_first:
                return day_first or month_first
            self.unparseable.append((row, text))
            return None

        for fmt in _NAMED_MONTH_FORMATS:
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        self.unparseable.append((row, text))
        return None

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def warnings(self) -> list[dict[str, Any]]:
        """``date_unparseable`` / ``date_ambiguous`` / ``date_convention_inferred``."""
        out: list[dict[str, Any]] = []
        if self.unparseable:
            shown = ", ".join(
                f"row {row} {text!r}" for row, text in self.unparseable[:_QUOTED]
            )
            more = len(self.unparseable) - min(len(self.unparseable), _QUOTED)
            out.append({
                "row": None,
                "code": "date_unparseable",
                "message": (
                    f"{len(self.unparseable)} drill date value(s) could not be "
                    f"read as a date and were left empty"
                ),
                "detail": (
                    f"These drill date cells are not a date this parser reads: "
                    f"{shown}{f' (and {more} more)' if more else ''}. They were "
                    f"stored empty rather than guessed at. Dates written as "
                    f"YYYY-MM-DD (or with a time, or as 05-Apr-2023) are "
                    f"always read; fix the cells and upload the file again."
                )[:900],
                "context": {
                    "count": len(self.unparseable),
                    "rows": [
                        {"row": row, "value": text}
                        for row, text in self.unparseable[:_LISTED]
                    ],
                },
            })
        if self.ambiguous:
            shown = "; ".join(
                f"row {row} {text!r} is {first:%d %b %Y} or {second:%d %b %Y}"
                for row, text, first, second in self.ambiguous[:_QUOTED]
            )
            more = len(self.ambiguous) - min(len(self.ambiguous), _QUOTED)
            mixed = self.convention == "mixed"
            out.append({
                "row": None,
                "code": "date_ambiguous",
                "message": (
                    f"{len(self.ambiguous)} drill date value(s) could be day/month "
                    f"or month/day and were left empty"
                ),
                "detail": (
                    "Both orders give a real date for these cells, and nothing in "
                    "the file says which one this export uses"
                    + (
                        " (the column contains both a day-first and a "
                        "month-first date)"
                        if mixed else ""
                    )
                    + f": {shown}{f' (and {more} more)' if more else ''}. They "
                    f"were stored empty rather than read day-first by default. "
                    f"Write the dates as YYYY-MM-DD and upload the file again."
                )[:900],
                "context": {
                    "count": len(self.ambiguous),
                    "rows": [
                        {
                            "row": row, "value": text,
                            "day_first": first.isoformat(),
                            "month_first": second.isoformat(),
                        }
                        for row, text, first, second in self.ambiguous[:_LISTED]
                    ],
                },
            })
        if self.inferred:
            order = "day-first (D/M/Y)" if self.convention == "dmy" else "month-first (M/D/Y)"
            proof_row, proof_text = self.evidence[self.convention or "dmy"]
            out.append({
                "row": None,
                "code": "date_convention_inferred",
                "severity": "info",
                "message": (
                    f"{len(self.inferred)} ambiguous drill date(s) were read "
                    f"{order}, as the rest of the column is"
                ),
                "detail": (
                    f"{len(self.inferred)} drill date cell(s) such as "
                    f"{self.inferred[0][1]!r} could be read either way. The "
                    f"column holds {proof_text!r} (row {proof_row}), which only "
                    f"{order} can explain, so the others were read {order} too."
                ),
                "context": {
                    "convention": self.convention,
                    "evidence_row": proof_row,
                    "count": len(self.inferred),
                },
            })
        return out
