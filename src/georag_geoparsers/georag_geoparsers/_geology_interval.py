"""Shared engine for the alteration and mineralization parsers.

``csv_alteration`` and ``csv_mineralization`` are thin wrappers over
:func:`parse_family`; the two tables differ in vocabulary and in which silver
columns they fill, not in how a geology log is read.

Two ways a table reaches these parsers
--------------------------------------
STANDALONE - an alteration log or a mineralization log of its own. Hole,
from, to and the family's name column are required; a row with no name but
other alteration/mineralization values is rejected (there is nothing to name
the observation); a row with nothing at all is an empty row and is skipped.

COMPANION (``companion=True``) - the same table is ALSO a lithology (or
alteration) log and carries alteration / mineralization columns beside the
lithology ones. One source row then feeds several silver tables, and the user
is never asked to split the file. In this mode:

* a row with no alteration/mineralization is simply not applicable (most
  rows of a lithology log); nothing is reported;
* a row whose hole or depths are unusable is skipped without a second
  complaint - the primary parser already rejected that row, with the reason;
* only columns that NAME the family are read (``Alteration``, ``Mineral1``,
  ``Min_Style``...). ``Comments`` / ``Description`` stay with the lithology,
  so one free-text cell is not stored against two tables.

What is never guessed
---------------------
* Values are stored as typed. Intensity ("Strong", "3", "S"), style, mineral
  names and grain size are text columns; nothing is mapped onto a vocabulary.
* A percentage is taken only when the cell is a number (optionally with a
  ``%``) inside 0-100. "trace", "<1", "3-5" and 140 are NOT converted: the
  column is left NULL, the text is kept in ``notes`` and one warning counts
  them. Ranges are not averaged.
* A column that says a percentage but numbers no mineral beside several
  numbered ones cannot be assigned to one of them; it is kept in ``notes`` of
  every row of that interval and warned about, never given to slot 1.
* A free-text ``Mineralization`` column is stored verbatim as the mineral and
  reported once (``mineralization_text_unsplit``); it is not split into
  minerals.
* ``silver.mineralization`` has no intensity column, ``silver.alteration`` has
  no style column: both are kept in ``notes`` ("intensity: strong") and
  counted, not dropped.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from georag_geoparsers._csv_io import (
    DEFAULT_NULL_VALUES,
    detect_delimiter,
    open_csv_with_encoding,
    read_csv_checked,
    transform_decimal_comma,
)
from georag_geoparsers._depth_units import convert_feet_columns
from georag_geoparsers._encoding import decode_warnings
from georag_geoparsers._geology_columns import (
    FAMILY_ALTERATION,
    FAMILY_MINERALIZATION,
    ROLE_FORM,
    ROLE_GRAIN,
    ROLE_GROUP_PCT,
    ROLE_INTENSITY,
    ROLE_MINERALS,
    ROLE_NAME,
    ROLE_NOTES,
    ROLE_PCT,
    ROLE_STYLE,
    SLOT_LEVEL_ROLES,
    FamilyColumn,
    classify_header,
    is_none_token,
    weak_column,
)
from georag_geoparsers._header_match import build_column_map, normalize_header
from georag_geoparsers._hole_id import canonicalize, suggest_collisions
from georag_geoparsers._vendor_aliases import merge_vendor_aliases

logger = logging.getLogger(__name__)

_KEY_FIELDS = ("hole_id", "from_depth", "to_depth")

_CODE_ENCODING_NON_UTF8 = "encoding_non_utf8"
_CODE_MISSING_REQUIRED = "missing_required"
_CODE_NUMERIC_CAST = "numeric_cast_failed"
_CODE_DEPTH_ORDER = "depth_order_invalid"
_CODE_DEPTH_NEG = "depth_negative"
_CODE_RANGE = "range_check_failed"
_CODE_DECIMAL_COMMA = "decimal_comma_detected"

#: Inclusive depth bounds, the same as the lithology parser's.
_DEPTH_MAX = 10_000.0

_DECIMAL_COMMA = re.compile(r"^\d+,\d+$")
_MINERAL_SPLIT = re.compile(r"\s*[;,/|]\s*")

#: Column names a user's confirmed mapping may name, per family, with the
#: (role, slot) each means. Slot 1: a user naming "the" alteration column means
#: the first (only) one.
_USER_CANONICALS: dict[str, dict[str, tuple[str, int]]] = {
    FAMILY_ALTERATION: {
        "alteration_type": (ROLE_NAME, 1),
        "intensity": (ROLE_INTENSITY, 0),
        "minerals": (ROLE_MINERALS, 0),
        "notes": (ROLE_NOTES, 0),
        "style": (ROLE_STYLE, 0),
    },
    FAMILY_MINERALIZATION: {
        "mineral": (ROLE_NAME, 1),
        "abundance_pct": (ROLE_PCT, 1),
        "form": (ROLE_FORM, 0),
        "grain_size": (ROLE_GRAIN, 0),
        "notes": (ROLE_NOTES, 0),
        "intensity": (ROLE_INTENSITY, 0),
    },
}

_TYPE_FIELD = {FAMILY_ALTERATION: "alteration_type", FAMILY_MINERALIZATION: "mineral"}


@dataclass
class FamilyParseResult:
    """A completed alteration / mineralization parse."""

    records: list
    total_rows: int
    valid_rows: int
    skipped_rows: int
    unmapped_columns: list
    column_map: dict
    skipped_details: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    detected_encoding: str = "utf-8"
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def parse_quality_pct(self) -> float:
        if self.total_rows == 0:
            return 0.0
        return round(self.valid_rows / self.total_rows * 100, 2)


# ---------------------------------------------------------------------------
# Column plan
# ---------------------------------------------------------------------------

@dataclass
class ColumnPlan:
    """Which header plays which role, worked out once per table."""

    key_map: dict[str, str]
    #: header -> what it means, for every family column that was claimed.
    assigned: dict[str, FamilyColumn]
    #: canonical name -> header, the shape every parse result carries.
    column_map: dict[str, str]
    unmapped: list[str]
    #: name slots present, ascending (an unnumbered name is slot 1).
    name_slots: list[int]
    group_columns: list[tuple[str, FamilyColumn]]

    def by_role(self, role: str) -> list[tuple[str, FamilyColumn]]:
        return [(h, c) for h, c in self.assigned.items() if c.role == role]

    def header_for(self, role: str, slot: int) -> str | None:
        """The header for a role in a slot; ``slot=0`` means interval-level."""
        for header, col in self.assigned.items():
            if col.role == ROLE_NOTES and col.group:
                continue  # a labelled note, see labelled_notes()
            if col.role == role and (col.slot or (1 if role == ROLE_NAME else 0)) == slot:
                return header
        return None

    def labelled_notes(self) -> list[tuple[str, str]]:
        """``(header, label)`` of columns kept in notes under their own name.

        A free-text ``Mineralization`` column that sits beside a named-mineral
        column (``Mineral1``) cannot be the mineral, and calling it a style or a
        form would be a guess. Its text is kept in ``notes`` prefixed with the
        column name, which is what the geologist called it.
        """
        return [
            (h, c.group) for h, c in self.assigned.items()
            if c.role == ROLE_NOTES and c.group
        ]


def _canonical_name(family: str, col: FamilyColumn) -> str:
    slot = col.slot or (1 if col.role == ROLE_NAME else 0)
    if family == FAMILY_ALTERATION:
        base = {
            ROLE_NAME: "alteration_type", ROLE_INTENSITY: "intensity",
            ROLE_MINERALS: "minerals", ROLE_NOTES: "notes", ROLE_STYLE: "style",
        }[col.role]
    else:
        base = {
            ROLE_NAME: "mineral", ROLE_PCT: "abundance_pct", ROLE_FORM: "form",
            ROLE_GRAIN: "grain_size", ROLE_NOTES: "notes",
            ROLE_INTENSITY: "intensity", ROLE_GROUP_PCT: "sulphide_pct",
        }[col.role]
    if col.role == ROLE_GROUP_PCT or (col.role == ROLE_NOTES and col.group):
        return f"{base}:{col.group}"
    return base if slot <= 1 else f"{base}_{slot}"


def plan_columns(
    columns: list[str],
    family: str,
    *,
    base_aliases: dict[str, list[str]],
    vendor_aliases: dict[str, list[str]] | None,
    companion: bool,
) -> ColumnPlan:
    """Assign the file's headers to hole / depth keys and to family roles."""
    key_aliases = merge_vendor_aliases(
        {f: base_aliases[f] for f in _KEY_FIELDS},
        {f: v for f, v in (vendor_aliases or {}).items() if f in _KEY_FIELDS},
    )
    key_map, _ = build_column_map(columns, key_aliases)
    claimed: set[str] = set(key_map.values())

    assigned: dict[str, FamilyColumn] = {}
    taken: set[tuple[str, int]] = set()  # (role, slot) already filled

    def _assign(header: str, col: FamilyColumn) -> None:
        slot = col.slot or (1 if col.role == ROLE_NAME else 0)
        key = (col.role, slot)
        if col.role != ROLE_GROUP_PCT and key in taken:
            if col.free_text:
                assigned[header] = FamilyColumn(
                    family, ROLE_NOTES, 0, strong=True, group=header,
                )
                claimed.add(header)
            return  # a second column for the same job: the first in file order keeps it
        taken.add(key)
        assigned[header] = col
        claimed.add(header)

    # 1. A mapping the user confirmed beats every spelling below.
    user_specs = _USER_CANONICALS[family]
    for canonical, aliases in (vendor_aliases or {}).items():
        spec = user_specs.get(canonical)
        if spec is None:
            continue
        wanted = {normalize_header(a) for a in aliases}
        for header in columns:
            if header not in claimed and normalize_header(header) in wanted:
                _assign(header, FamilyColumn(family, spec[0], spec[1], strong=True))
                break

    # 2. Headers that name the family.
    for header in columns:
        if header in claimed:
            continue
        col = classify_header(header, family)
        if col is not None:
            _assign(header, col)

    # 3. Once the table is known to hold the family (not a companion), its
    #    generic attribute columns are read as well.
    if not companion:
        for header in columns:
            if header in claimed:
                continue
            col = weak_column(header, family)
            if col is not None:
                _assign(header, col)

    # A slot-2+ attribute with no name in that slot belongs to nothing.
    name_slots = sorted({
        (c.slot or 1) for c in assigned.values() if c.role == ROLE_NAME
    })
    for header, col in list(assigned.items()):
        if col.role in (ROLE_NAME, ROLE_GROUP_PCT) or col.slot == 0:
            continue
        if col.slot not in name_slots:
            del assigned[header]
            claimed.discard(header)

    column_map = dict(key_map)
    for header, col in assigned.items():
        column_map[_canonical_name(family, col)] = header
    group_columns = [(h, c) for h, c in assigned.items() if c.role == ROLE_GROUP_PCT]
    unmapped = [c for c in columns if c not in claimed]
    return ColumnPlan(
        key_map=key_map, assigned=assigned, column_map=column_map,
        unmapped=unmapped, name_slots=name_slots, group_columns=group_columns,
    )


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------

def _cell(raw: dict, header: str | None) -> str:
    if header is None:
        return ""
    value = raw.get(header)
    if value is None:
        return ""
    return " ".join(str(value).split())


def _float(text: str) -> float | None:
    try:
        return float(text.strip())
    except (TypeError, ValueError):
        # Not an error: the caller decides whether a non-numeric cell rejects
        # the row (a depth) or is kept as text with a warning (a percentage).
        logger.debug("geology_interval: non-numeric cell")
        return None


def parse_percentage(text: str) -> tuple[float | None, bool]:
    """``(value, in_range)`` for an abundance cell.

    ``value`` is None when the cell is not a plain number (optionally with a
    trailing ``%``): "trace", "<1", "3-5". ``in_range`` is False for a number
    outside 0-100, which the silver column forbids; the caller keeps the text.
    """
    t = text.strip()
    if t.endswith("%"):
        t = t[:-1].strip()
    if _DECIMAL_COMMA.match(t):
        t = t.replace(",", ".")
    value = _float(t)
    if value is None or value != value:  # NaN
        return None, True
    return value, 0.0 <= value <= 100.0


def _split_minerals(text: str) -> list[str] | None:
    parts = [p.strip() for p in _MINERAL_SPLIT.split(text) if p.strip()]
    return parts or None


class _Tally:
    """A count and a few raw examples, for one warning."""

    _MAX_EXAMPLES = 3
    _MAX_CHARS = 40

    def __init__(self) -> None:
        self.count = 0
        self.examples: list[str] = []

    def add(self, raw: Any) -> None:
        self.count += 1
        text = " ".join(str(raw).split())[: self._MAX_CHARS]
        if text not in self.examples and len(self.examples) < self._MAX_EXAMPLES:
            self.examples.append(text)

    def __bool__(self) -> bool:
        return self.count > 0


def _warn(code: str, message: str, detail: str, tally: _Tally | None = None) -> dict:
    out: dict[str, Any] = {
        "row": None, "code": code, "message": message, "detail": detail[:900],
    }
    if tally is not None:
        out["count"] = tally.count
        out["examples"] = tally.examples
    return out


def _reject(row_num: int, raw: dict, code: str, reason: str,
            expected: str, actual: Any, suggestion: str) -> dict:
    return {
        "row": row_num,
        "code": code,
        "reason": f"row {row_num}: {reason}",
        "raw": raw,
        "expected": expected,
        "actual": actual,
        "suggestion": suggestion,
    }


class _Counters:
    def __init__(self) -> None:
        self.abundance_text = _Tally()
        self.abundance_out_of_range = _Tally()
        self.free_text_mineral = _Tally()
        self.free_text_in_notes = _Tally()
        self.intensity_in_notes = _Tally()
        self.style_in_notes = _Tally()
        self.slot_value_unassigned = _Tally()
        self.companion_geometry_skipped = 0


def _geometry(
    row_num: int, raw: dict, key_map: dict[str, str],
) -> tuple[tuple[str, float, float] | None, dict | None]:
    """``((hole, from, to), None)`` or ``(None, rejection)``."""
    hole = _cell(raw, key_map.get("hole_id"))
    if not hole:
        return None, _reject(
            row_num, raw, _CODE_MISSING_REQUIRED, "missing required field 'hole_id'",
            "non-empty value for 'hole_id'", None,
            "Ensure the 'hole_id' column is populated, or map it explicitly.",
        )
    values: dict[str, float] = {}
    for name in ("from_depth", "to_depth"):
        text = _cell(raw, key_map.get(name))
        if not text:
            return None, _reject(
                row_num, raw, _CODE_MISSING_REQUIRED,
                f"missing required field '{name}'",
                f"non-empty value for '{name}'", None,
                f"Ensure the '{name}' column is populated, or map it explicitly.",
            )
        number = _float(text.replace(",", ".") if _DECIMAL_COMMA.match(text) else text)
        if number is None:
            return None, _reject(
                row_num, raw, _CODE_NUMERIC_CAST,
                f"cannot cast required numeric field '{name}' value '{text}'",
                "numeric value", {"field": name, "value": text},
                "Remove text units from the value cell.",
            )
        values[name] = number
    from_d, to_d = values["from_depth"], values["to_depth"]
    if from_d < 0:
        return None, _reject(
            row_num, raw, _CODE_DEPTH_NEG, f"from_depth {from_d} must be >= 0",
            "from_depth >= 0", {"from_depth": from_d},
            "Negative downhole depth is likely a sign-convention error.",
        )
    if to_d <= from_d:
        return None, _reject(
            row_num, raw, _CODE_DEPTH_ORDER,
            f"to_depth {to_d} must be > from_depth {from_d}",
            "to_depth > from_depth", {"from_depth": from_d, "to_depth": to_d},
            "Swap the from/to columns or check data entry.",
        )
    if from_d > _DEPTH_MAX or to_d > _DEPTH_MAX:
        return None, _reject(
            row_num, raw, _CODE_RANGE,
            f"depth out of range [0, {_DEPTH_MAX:g}]",
            f"depths in [0, {_DEPTH_MAX:g}]", {"from_depth": from_d, "to_depth": to_d},
            "Check the depth units.",
        )
    return (hole, from_d, to_d), None


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------

def _slot_value(
    plan: ColumnPlan, raw: dict, role: str, slot: int, n_slots: int,
) -> tuple[str, bool]:
    """``(text, unassigned)`` for a role in a slot.

    A numbered column is that slot's. An unnumbered SLOT-LEVEL role
    (intensity, percentage) is assigned to slot 1 only when there is exactly
    one name slot; with several it cannot be told which the value describes,
    so it is returned as ``unassigned`` for the caller to keep in notes.
    An unnumbered interval-level role (style, grain, notes...) describes the
    interval and applies to every slot.
    """
    numbered = plan.header_for(role, slot)
    if numbered is not None:
        return _cell(raw, numbered), False
    bare = plan.header_for(role, 0)
    if bare is None:
        return "", False
    text = _cell(raw, bare)
    if role in SLOT_LEVEL_ROLES and n_slots > 1:
        return text, bool(text)
    return text, False


def _alteration_rows(
    plan: ColumnPlan, raw: dict, geom: tuple[str, float, float],
    row_num: int, counters: _Counters,
) -> tuple[list[dict], bool]:
    """``(records, saw_other_content)`` for one source row."""
    hole, from_d, to_d = geom
    n_slots = len(plan.name_slots)
    records: list[dict] = []
    for slot in plan.name_slots:
        name = _cell(raw, plan.header_for(ROLE_NAME, slot))
        if not name or is_none_token(name, FAMILY_ALTERATION):
            continue
        notes: list[str] = []
        intensity, unassigned = _slot_value(plan, raw, ROLE_INTENSITY, slot, n_slots)
        if unassigned:
            if slot == plan.name_slots[0]:   # one source cell, however many rows
                counters.slot_value_unassigned.add(intensity)
            notes.append(f"intensity (not assigned to one alteration): {intensity}")
            intensity = ""
        minerals_text, _ = _slot_value(plan, raw, ROLE_MINERALS, slot, n_slots)
        note_text, _ = _slot_value(plan, raw, ROLE_NOTES, slot, n_slots)
        style_text, _ = _slot_value(plan, raw, ROLE_STYLE, slot, n_slots)
        if note_text:
            notes.insert(0, note_text)
        if style_text:
            counters.style_in_notes.add(style_text)
            notes.append(f"style: {style_text}")
        records.append({
            "hole_id": hole,
            "hole_id_canonical": canonicalize(hole),
            "from_depth": from_d,
            "to_depth": to_d,
            "alteration_type": name,
            "intensity": intensity or None,
            "minerals": _split_minerals(minerals_text) if minerals_text else None,
            "notes": "; ".join(notes) if notes else None,
            "_source_row": row_num,
        })
    if records:
        return records, False
    # Nothing named. Did the row still say something about an alteration
    # (an intensity, minerals) that a standalone table should not swallow?
    names = [_cell(raw, plan.header_for(ROLE_NAME, s)) for s in plan.name_slots]
    said_none = any(n and is_none_token(n, FAMILY_ALTERATION) for n in names)
    other = any(
        _cell(raw, h)
        for h, c in plan.assigned.items()
        if c.role in (ROLE_INTENSITY, ROLE_MINERALS, ROLE_STYLE)
    )
    return [], other and not said_none


def _abundance(
    text: str, counters: _Counters, notes: list[str],
) -> float | None:
    if not text:
        return None
    value, in_range = parse_percentage(text)
    if value is not None and in_range:
        return value
    if value is None:
        counters.abundance_text.add(text)
    else:
        counters.abundance_out_of_range.add(text)
    notes.append(f"abundance: {text}")
    return None


def _mineralization_rows(
    plan: ColumnPlan, raw: dict, geom: tuple[str, float, float],
    row_num: int, counters: _Counters,
) -> tuple[list[dict], bool]:
    hole, from_d, to_d = geom
    n_slots = len(plan.name_slots)
    records: list[dict] = []

    def _record(mineral: str, abundance: float | None, form: str, grain: str,
                notes: list[str]) -> dict:
        return {
            "hole_id": hole,
            "hole_id_canonical": canonicalize(hole),
            "from_depth": from_d,
            "to_depth": to_d,
            "mineral": mineral,
            "abundance_pct": abundance,
            "form": form or None,
            "grain_size": grain or None,
            "notes": "; ".join(notes) if notes else None,
            "_source_row": row_num,
        }

    for slot in plan.name_slots:
        name_header = plan.header_for(ROLE_NAME, slot)
        name = _cell(raw, name_header)
        if not name or is_none_token(name, FAMILY_MINERALIZATION):
            continue
        notes: list[str] = []
        pct_text, unassigned = _slot_value(plan, raw, ROLE_PCT, slot, n_slots)
        if unassigned:
            if slot == plan.name_slots[0]:   # one source cell, however many rows
                counters.slot_value_unassigned.add(pct_text)
            notes.append(f"abundance (not assigned to one mineral): {pct_text}")
            pct_text = ""
        note_text, _ = _slot_value(plan, raw, ROLE_NOTES, slot, n_slots)
        if note_text:
            notes.insert(0, note_text)
        abundance = _abundance(pct_text, counters, notes)
        form, _ = _slot_value(plan, raw, ROLE_FORM, slot, n_slots)
        grain, _ = _slot_value(plan, raw, ROLE_GRAIN, slot, n_slots)
        intensity, int_unassigned = _slot_value(plan, raw, ROLE_INTENSITY, slot, n_slots)
        if intensity:
            if int_unassigned:
                if slot == plan.name_slots[0]:
                    counters.slot_value_unassigned.add(intensity)
                notes.append(f"intensity (not assigned to one mineral): {intensity}")
            else:
                notes.append(f"intensity: {intensity}")
            counters.intensity_in_notes.add(intensity)
        for header, label in plan.labelled_notes():
            text = _cell(raw, header)
            if text and not is_none_token(text, FAMILY_MINERALIZATION):
                notes.append(f"{label}: {text}")
                counters.free_text_in_notes.add(text)
        if name_header is not None and plan.assigned[name_header].free_text:
            counters.free_text_mineral.add(name)
        records.append(_record(name, abundance, form, grain, notes))

    # "Sulphide%": a percentage of a group, not of one mineral.
    for header, col in plan.group_columns:
        text = _cell(raw, header)
        if not text or is_none_token(text, FAMILY_MINERALIZATION):
            continue
        notes = [f"group total as logged in '{header}', not a single mineral"]
        abundance = _abundance(text, counters, notes)
        if abundance == 0.0:
            continue  # zero sulphide: an observation of absence, not an occurrence
        label = (col.group or "sulphide").capitalize()
        records.append(_record(label, abundance, "", "", notes))

    if records:
        return records, False
    names = [_cell(raw, plan.header_for(ROLE_NAME, s)) for s in plan.name_slots]
    said_none = any(n and is_none_token(n, FAMILY_MINERALIZATION) for n in names)
    other = any(
        _cell(raw, h)
        for h, c in plan.assigned.items()
        if c.role in (ROLE_PCT, ROLE_FORM, ROLE_GRAIN, ROLE_INTENSITY)
    )
    return [], other and not said_none


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------

def _family_warnings(family: str, counters: _Counters) -> list[dict]:
    out: list[dict] = []
    if family == FAMILY_MINERALIZATION:
        if counters.abundance_text:
            n = counters.abundance_text.count
            out.append(_warn(
                "mineralization_abundance_not_numeric",
                f"{n} mineral abundance value(s) are not a plain number",
                f"{n} abundance cell(s) hold text such as "
                f"{', '.join(repr(e) for e in counters.abundance_text.examples)} "
                f"(trace / '<1' / a range), which cannot be stored as a "
                f"percentage without guessing. The percentage was left empty "
                f"and the original text was kept in the row's notes; the "
                f"rows still landed.",
                counters.abundance_text,
            ))
        if counters.abundance_out_of_range:
            n = counters.abundance_out_of_range.count
            out.append(_warn(
                "mineralization_abundance_out_of_range",
                f"{n} mineral abundance value(s) are outside 0-100%",
                f"{n} abundance cell(s) are numbers outside 0-100 "
                f"(e.g. {', '.join(repr(e) for e in counters.abundance_out_of_range.examples)}). "
                f"The percentage was left empty and the original value was "
                f"kept in the row's notes; the rows still landed.",
                counters.abundance_out_of_range,
            ))
        if counters.free_text_mineral:
            n = counters.free_text_mineral.count
            out.append(_warn(
                "mineralization_text_unsplit",
                f"{n} row(s) took a free-text 'Mineralization' cell as the mineral",
                f"The file's Mineralization column holds free text "
                f"(e.g. {', '.join(repr(e) for e in counters.free_text_mineral.examples)}). "
                f"It was stored as written in the mineral field and not split "
                f"into individual minerals or percentages. A 'Mineral1' / "
                f"'Min1_%' column pair gives one row per mineral.",
                counters.free_text_mineral,
            ))
        if counters.free_text_in_notes:
            n = counters.free_text_in_notes.count
            out.append(_warn(
                "mineralization_text_in_notes",
                f"{n} free-text mineralization value(s) were kept in notes",
                f"The file has a named-mineral column AND a free-text "
                f"Mineralization column "
                f"(e.g. {', '.join(repr(e) for e in counters.free_text_in_notes.examples)}). "
                f"The free text was kept in each row's notes, labelled with "
                f"the column name, rather than being read as a style or a "
                f"mineral.",
                counters.free_text_in_notes,
            ))
        if counters.intensity_in_notes:
            n = counters.intensity_in_notes.count
            out.append(_warn(
                "mineralization_intensity_in_notes",
                f"{n} mineralization intensity value(s) were kept in notes",
                "The mineralization table has no intensity column, so the "
                "intensity was written into each row's notes "
                "('intensity: ...') instead of being dropped.",
                counters.intensity_in_notes,
            ))
    else:
        if counters.style_in_notes:
            n = counters.style_in_notes.count
            out.append(_warn(
                "alteration_style_in_notes",
                f"{n} alteration style value(s) were kept in notes",
                "The alteration table has no style column, so the style was "
                "written into each row's notes ('style: ...') instead of "
                "being dropped.",
                counters.style_in_notes,
            ))
    if counters.slot_value_unassigned:
        n = counters.slot_value_unassigned.count
        what = "alteration" if family == FAMILY_ALTERATION else "mineral"
        out.append(_warn(
            f"{family}_value_unassigned",
            f"{n} intensity/percentage value(s) could not be tied to one {what}",
            f"These rows name more than one {what} but the intensity / "
            f"percentage column carries no number (Alt1_Int, Min2_%), so it "
            f"cannot be said which {what} it describes. The value was kept "
            f"in the notes of every {what} row of that interval instead of "
            f"being given to the first by guess.",
            counters.slot_value_unassigned,
        ))
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _empty(
    *, total_rows: int, columns: list[str], warnings: list, encoding: str,
    provenance: dict, skipped: list | None = None, column_map: dict | None = None,
    skipped_rows: int = 0,
) -> FamilyParseResult:
    return FamilyParseResult(
        records=[], total_rows=total_rows, valid_rows=0,
        skipped_rows=skipped_rows, unmapped_columns=list(columns),
        column_map=column_map or {}, skipped_details=skipped or [],
        warnings=warnings, detected_encoding=encoding, provenance=provenance,
    )


def parse_family(
    source: str | Path | IO,
    *,
    family: str,
    base_aliases: dict[str, list[str]],
    parser_name: str,
    parser_version: str,
    null_values: list | None = None,
    vendor_aliases: dict[str, list[str]] | None = None,
    companion: bool = False,
) -> FamilyParseResult:
    """Parse one alteration / mineralization table (see the module docstring)."""
    warnings: list = []
    source_file = str(source) if isinstance(source, (str, Path)) else "<stream>"
    all_nulls = list(set(DEFAULT_NULL_VALUES + (null_values or [])))

    try:
        stream, encoding, sha256_hex, _byte_count = open_csv_with_encoding(source)
        content = stream.getvalue()
        # decode_warnings, not the inline `name.replace("-", "")` test this
        # used to carry: charset-normalizer's "utf_8" never matched it, so a
        # UTF-8 file with one accented letter was reported as not UTF-8 (the
        # five drill parsers were fixed for that on 2026-09-29; this was not).
        warnings.extend(decode_warnings(encoding, content))
        delimiter = detect_delimiter(content, default=",")
        if delimiter != ",":
            warnings.append({
                "row": None, "code": "delimiter_non_comma",
                "message": (
                    f"detected delimiter {delimiter!r} (non-comma) — Polars "
                    f"read_csv configured accordingly"
                ),
                "context": {"delimiter": delimiter},
            })
        df, ragged = read_csv_checked(
            content, separator=delimiter, null_values=all_nulls,
        )
        if not companion:
            # A companion parse reads the same rows the primary parse already
            # reported; saying it twice would double the count on the run.
            warnings.extend(ragged.warnings())
        df, transformed = transform_decimal_comma(df)
        if transformed:
            warnings.append({
                "row": None, "code": _CODE_DECIMAL_COMMA,
                "message": f"decimal-comma transform applied to columns: {transformed!r}",
                "context": {"encoding": encoding, "columns": transformed},
            })
    except Exception as exc:
        logger.error("Failed to read %s source: %s", parser_name, exc)
        raise

    columns: list[str] = df.columns
    total_rows = len(df)
    provenance = {
        "source_file": source_file,
        "source_file_sha256": sha256_hex,
        "parser_name": parser_name,
        "parser_version": parser_version,
        "source_col_map": {},
        "companion": companion,
    }

    plan = plan_columns(
        columns, family, base_aliases=base_aliases,
        vendor_aliases=vendor_aliases, companion=companion,
    )
    provenance["source_col_map"] = plan.column_map
    type_field = _TYPE_FIELD[family]
    has_name = bool(plan.name_slots) or bool(plan.group_columns)

    if companion and not has_name:
        # Not an error: the table just holds no such columns.
        return _empty(
            total_rows=total_rows, columns=plan.unmapped, warnings=[],
            encoding=encoding, provenance=provenance, column_map=plan.column_map,
        )

    missing = [f for f in _KEY_FIELDS if f not in plan.key_map]
    if not has_name:
        missing.append(type_field)
    if missing:
        needed = "{" + ", ".join(repr(m) for m in missing) + "}"
        return _empty(
            total_rows=total_rows, columns=plan.unmapped, warnings=warnings,
            encoding=encoding, provenance=provenance, column_map=plan.column_map,
            skipped_rows=total_rows,
            skipped=[{
                "row": None,
                "code": _CODE_MISSING_REQUIRED,
                "reason": f"file-level: missing required column mapping(s): {needed}",
                "raw": {},
                "expected": f"columns matching {needed}",
                "actual": None,
                "suggestion": (
                    "Rename the headers to a recognised spelling "
                    "(Alteration / Alt_Type; Mineral1 / Mineralization) or map "
                    "the columns explicitly."
                ),
            }],
        )
    if companion and any(f not in plan.key_map for f in _KEY_FIELDS):
        return _empty(
            total_rows=total_rows, columns=plan.unmapped, warnings=[],
            encoding=encoding, provenance=provenance, column_map=plan.column_map,
        )

    # "From_ft" / "To_ft" -> metres (GIS-3). Converted in companion mode
    # too — the values must agree with the lithology rows they sit beside —
    # but reported once, by the primary parse of the same columns.
    df, unit_warning = convert_feet_columns(
        df,
        columns=plan.key_map,
        headers=plan.key_map,
        fields=("from_depth", "to_depth"),
        parser=parser_name,
    )
    if unit_warning is not None and not companion:
        warnings.append(unit_warning)

    build = _alteration_rows if family == FAMILY_ALTERATION else _mineralization_rows
    counters = _Counters()
    records: list[dict] = []
    skipped: list[dict] = []
    source_rows = 0

    for row_num, raw in enumerate(df.to_dicts(), start=2):
        if row_num in ragged.rows:
            # Wider than the header: its values are shifted. The primary
            # parse reports the row; a companion parse just leaves it out.
            if not companion:
                ragged.skip(row_num, skipped)
            continue
        geom, rejection = _geometry(row_num, raw, plan.key_map)
        if geom is None:
            if companion:
                counters.companion_geometry_skipped += 1
                continue
            # A standalone row that says nothing at all is an empty row.
            has_content = any(
                _cell(raw, h) for h in plan.assigned
            )
            if has_content:
                skipped.append(rejection)
            continue
        rows, other_content = build(plan, raw, geom, row_num, counters)
        if rows:
            records.extend(rows)
            source_rows += 1
        elif other_content and not companion:
            skipped.append(_reject(
                row_num, raw, _CODE_MISSING_REQUIRED,
                f"the row has {family} values but no '{type_field}'",
                f"non-empty value for '{type_field}'", None,
                f"Name the {'alteration' if family == FAMILY_ALTERATION else 'mineral'} "
                f"in its own column.",
            ))

    warnings.extend(_family_warnings(family, counters))

    if not companion:
        raw_ids = [r["hole_id"] for r in records if r.get("hole_id")]
        for collision in suggest_collisions(raw_ids):
            warnings.append({
                "row": None,
                "code": "hole_id_canonical_collision",
                "message": (
                    f"{collision['a']!r} and {collision['b']!r} both "
                    f"canonicalize to {collision['canonical']!r}"
                ),
                "context": {
                    "raw_a": collision["a"], "raw_b": collision["b"],
                    "canonical": collision["canonical"],
                },
            })

    result = FamilyParseResult(
        records=records,
        total_rows=total_rows,
        valid_rows=source_rows,
        skipped_rows=len(skipped),
        unmapped_columns=plan.unmapped,
        column_map=plan.column_map,
        skipped_details=skipped,
        warnings=warnings,
        detected_encoding=encoding,
        provenance=provenance,
    )
    logger.info(
        "%s parse complete — total: %d, source rows used: %d, records: %d, "
        "skipped: %d, companion: %s",
        parser_name, total_rows, source_rows, len(records), len(skipped), companion,
    )
    return result
