"""Which north an azimuth is measured from — the one vocabulary.

Kyle (2026-09-29): azimuths are corrected only when the data DECLARES its
reference. Two places can declare it:

* the project — ``silver.projects.orientation_reference`` (BOH / TOH, the
  core-orientation mark, declare nothing; grid / true / magnetic do);
* a survey file — an azimuth-reference column (``Azimuth_Ref``,
  ``Az_Reference``, ``North_Ref`` ...), stored per station in
  ``silver.surveys.azimuth_reference`` and preferred over the project's.

Both are read through :func:`canonical_azimuth_reference`, so a spelling
learned once is understood everywhere, and the stored values are exactly the
three the ``silver.surveys`` CHECK allows.

Deliberately NOT guessed: an unrecognised value (``UTM``, ``local``, a
number) returns ``None``. The parser blanks it and says so; it is never read
as grid north because grid is the no-correction default — that would make
an unreadable declaration indistinguishable from an absent one.
"""

from __future__ import annotations

#: The canonical references, and the values silver.surveys.azimuth_reference
#: and silver.projects.orientation_reference store for them.
TRUE = "true"
MAGNETIC = "magnetic"
GRID = "grid"
AZIMUTH_REFERENCES: tuple[str, ...] = (TRUE, MAGNETIC, GRID)

_SPELLINGS: dict[str, frozenset[str]] = {
    TRUE: frozenset({
        "true", "true_north", "truenorth", "tn", "t",
        "geographic", "geographic_north",
    }),
    MAGNETIC: frozenset({
        "magnetic", "magnetic_north", "magneticnorth", "mag", "mag_north",
        "mn", "m",
    }),
    GRID: frozenset({"grid", "grid_north", "gridnorth", "gn", "g"}),
}


def _token(raw: object) -> str:
    return (
        str(raw).strip().lower()
        .replace(" ", "_").replace("-", "_").replace(".", "")
    )


def canonical_azimuth_reference(raw: object) -> str | None:
    """``'true' | 'magnetic' | 'grid'`` for a recognised spelling, else None.

    ``None``, blank, and the core-orientation marks (``BOH`` / ``TOH``) all
    return None: none of them says which north an azimuth is measured from.
    """
    if raw is None:
        return None
    token = _token(raw)
    if not token:
        return None
    for canonical, spellings in _SPELLINGS.items():
        if token in spellings:
            return canonical
    return None
