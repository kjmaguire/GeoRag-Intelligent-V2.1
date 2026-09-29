"""Optional enum-like fields: keep the row, blank the value, say so.

## Why this exists

The interval and survey parsers each carry a fixed vocabulary for a few
OPTIONAL descriptive columns: lithology ``grain_size`` / ``hardness`` /
``weathering``, survey ``survey_method``, sample ``qaqc_type``. Until now a
value outside the vocabulary rejected the WHOLE row. Real logs are free text
in exactly those columns ("porphyritic", "Reflex EZ-Shot", "CRM"), so one
unfamiliar word in an optional column threw away the interval - and with it
the hole's strip log, or its downhole survey - while the required fields
(hole, depths, lithology code) were perfectly good. That trade is backwards:
losing a row is far worse than losing one descriptive attribute.

Decision (Kyle, 2026-09-29): keep the row, set just that field to NULL,
and record it. REQUIRED fields are untouched - a row still cannot be
written without its hole id, depths or lithology code.

The recording is the point. Blanking silently would be the same data loss
with better manners, so every parser that blanks a value returns one
``optional_values_blanked`` warning carrying, per field, how many values
were blanked and a few example raw values - enough for a geologist to see
whether it was noise ("n/a") or a real vocabulary gap ("porphyritic").

## What is NOT done here

Nothing is mapped to the nearest allowed value. "porphyritic" is not a grain
size, and guessing "Medium" would invent a measurement. The only leniency is
spelling: a value that differs from an allowed one only in case or
whitespace ("very  coarse", "HARD") is the same value and is canonicalised,
not blanked.
"""

from __future__ import annotations

from typing import Any

#: The warning ``code`` ingest_tabular and the Ingestion Runs page key on.
CODE_OPTIONAL_VALUES_BLANKED = "optional_values_blanked"

#: Examples kept per field, and the width each is cut to. Raw cell text goes
#: into a warning that is persisted and shown to the user, so both are
#: bounded.
_MAX_EXAMPLES = 3
_MAX_EXAMPLE_CHARS = 40


def _fold(value: str) -> str:
    """Case-fold and collapse internal whitespace for comparison."""
    return " ".join(value.split()).casefold()


def canonical_choice(value: str | None, valid: frozenset[str]) -> str | None:
    """The allowed spelling that *value* denotes, else ``None``.

    ``None`` means "not one of the allowed values" - the caller decides
    whether that blanks the field. A blank input is also ``None``; callers
    check for blankness first so an empty cell is never reported as an
    unrecognised value.
    """
    if value is None:
        return None
    wanted = _fold(str(value))
    if not wanted:
        return None
    for allowed in valid:
        if _fold(allowed) == wanted:
            return allowed
    return None


class BlankedValues:
    """Collects the optional values one parse blanked, per field."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._examples: dict[str, list[str]] = {}

    def add(self, field_name: str, raw: Any) -> None:
        self._counts[field_name] = self._counts.get(field_name, 0) + 1
        text = " ".join(str(raw).split())[:_MAX_EXAMPLE_CHARS]
        examples = self._examples.setdefault(field_name, [])
        if text not in examples and len(examples) < _MAX_EXAMPLES:
            examples.append(text)

    def __bool__(self) -> bool:
        return bool(self._counts)

    @property
    def total(self) -> int:
        return sum(self._counts.values())

    def as_warning(self, *, parser: str) -> dict[str, Any] | None:
        """The ``optional_values_blanked`` warning, or ``None`` if nothing was."""
        if not self._counts:
            return None

        fields: dict[str, dict[str, Any]] = {
            name: {"count": count, "examples": list(self._examples[name])}
            for name, count in sorted(self._counts.items())
        }
        parts = [
            f"{name}: {info['count']} value(s)"
            f" (e.g. {', '.join(repr(e) for e in info['examples'])})"
            for name, info in fields.items()
        ]
        names = ", ".join(fields)
        return {
            "row": None,
            "code": CODE_OPTIONAL_VALUES_BLANKED,
            "message": (
                f"{self.total} optional value(s) outside the accepted list "
                f"were left blank ({names}); the rows were kept"
            ),
            "detail": (
                "Some optional columns hold values the platform does not "
                "recognise, so those cells were stored empty instead of "
                "rejecting the whole row - "
                + "; ".join(parts)
                + ". Every row still landed with its required fields. If "
                "these are real categories, tell us the vocabulary and they "
                "can be added."
            )[:900],
            "fields": fields,
            "context": {"parser": parser},
        }


__all__ = [
    "CODE_OPTIONAL_VALUES_BLANKED",
    "BlankedValues",
    "canonical_choice",
]
