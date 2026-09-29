"""Assay column headers: which element, which unit, and how to store it.

Why this exists (audit 2026-09-29, ING-3)
-----------------------------------------
``csv_sample`` recognised an assay column only when its header matched
``^(U3O8|Au|Ag|Cu|Pb|Zn|Ni|Fe|Ti|Li)_?(ppm|pct|ppb)?$``. Gold in g/t - the
industry's standard unit - and any multi-element ICP suite outside that
list (Mo, Co, W, Sn, PGEs, REEs, oxides) landed nowhere typed: ``Au (g/t)``,
``Ag_gpt``, ``Cu_%``, ``Pb ppm`` and ``Mo`` were all "unmapped". The platform
is for any company and any commodity, so the vocabulary is the periodic
table's assayable elements plus the oxides labs report, not one project's
list.

What a header resolves to
-------------------------
``parse_assay_header("Au (g/t)")`` -> element ``Au``, stored unit ``ppm``,
factor 1. Values are stored in one of three units only - ``ppm``, ``ppb``,
``pct`` - because those are what ``silver.assays_v2.value_ppm`` converts and
what the agent's assay tools read (keys like ``Au_ppm``, ``Cu_pct``):

    ppm, mg/kg, ug/g, g/t, gpt, g/tonne   -> ppm   (x1)
    ppb, ug/kg                            -> ppb   (x1)
    %, pct, percent, wt%                  -> pct   (x1)
    oz/t, opt, ozt (troy oz / short ton)  -> ppm   (x34.2857)

A header that names only the element (``Mo``) is ASSUMED ppm and reported as
an assumption - it is not detected. The same for an element with only a lab
method code (``Au_FA``, ``Au-AA24``).

What is NOT an assay column
---------------------------
* Anything whose first token is not an element, oxide or element name:
  ``AuEq_gpt`` (a calculated equivalent), ``eU3O8`` (radiometric), ``Hole``.
* A single-letter symbol with no unit (``Y`` is a northing as often as it
  is yttrium; ``S``/``U``/``W`` need a unit to be read as sulphur / uranium /
  tungsten).
* An element followed by words that are neither a unit nor a lab method code
  (``As received wt``, ``Co_ordinate``, ``Cu_Eq``).
* Two different units in one header.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Elements a geochemical lab reports. Deliberately not the whole table: no
#: noble gases, no H/N/O, nothing synthetic. Canonical casing is the value.
_ELEMENTS: tuple[str, ...] = (
    "Ag", "Al", "As", "Au", "B", "Ba", "Be", "Bi", "Br", "C", "Ca", "Cd",
    "Ce", "Cl", "Co", "Cr", "Cs", "Cu", "Dy", "Er", "Eu", "F", "Fe", "Ga",
    "Gd", "Ge", "Hf", "Hg", "Ho", "In", "Ir", "K", "La", "Li", "Lu", "Mg",
    "Mn", "Mo", "Na", "Nb", "Nd", "Ni", "Os", "P", "Pb", "Pd", "Pr", "Pt",
    "Rb", "Re", "Rh", "Ru", "S", "Sb", "Sc", "Se", "Si", "Sm", "Sn", "Sr",
    "Ta", "Tb", "Te", "Th", "Ti", "Tl", "Tm", "U", "V", "W", "Y", "Yb", "Zn",
    "Zr",
)

#: Oxides and compound analytes labs report as their own column.
_OXIDES: tuple[str, ...] = (
    "U3O8", "V2O5", "Li2O", "WO3", "MoS2", "SiO2", "Al2O3", "Fe2O3", "FeO",
    "Fe3O4", "CaO", "MgO", "Na2O", "K2O", "TiO2", "MnO", "P2O5", "Cr2O3",
    "BaO", "SrO", "ZrO2", "Nb2O5", "Ta2O5", "ThO2", "SnO2", "Cs2O", "Rb2O",
    "BeO", "CuO", "ZnO", "PbO", "NiO", "CoO", "TREO",
)

#: Common English names in headers ("Gold (g/t)", "Copper %").
_NAMES: dict[str, str] = {
    "gold": "Au", "silver": "Ag", "copper": "Cu", "lead": "Pb", "zinc": "Zn",
    "nickel": "Ni", "cobalt": "Co", "molybdenum": "Mo", "uranium": "U",
    "lithium": "Li", "iron": "Fe", "platinum": "Pt", "palladium": "Pd",
    "tungsten": "W", "tin": "Sn", "arsenic": "As", "antimony": "Sb",
    "bismuth": "Bi", "manganese": "Mn", "vanadium": "V", "titanium": "Ti",
    "chromium": "Cr", "barium": "Ba", "sulphur": "S", "sulfur": "S",
    "tellurium": "Te", "thorium": "Th", "tantalum": "Ta", "niobium": "Nb",
    "rhodium": "Rh", "mercury": "Hg", "cadmium": "Cd", "indium": "In",
    "gallium": "Ga", "germanium": "Ge", "beryllium": "Be", "cesium": "Cs",
    "caesium": "Cs", "rubidium": "Rb", "scandium": "Sc", "yttrium": "Y",
}

_ANALYTES: dict[str, str] = {
    **{e.lower(): e for e in _ELEMENTS},
    **{o.lower(): o for o in _OXIDES},
    **_NAMES,
}

#: Troy ounces per short ton -> grams per tonne (= ppm).
OZ_PER_TON_TO_PPM = 34.2857

#: Normalised unit token -> (stored unit, factor, what the source said).
_UNITS: dict[str, tuple[str, float, str]] = {
    "ppm": ("ppm", 1.0, "ppm"),
    "ppb": ("ppb", 1.0, "ppb"),
    "pct": ("pct", 1.0, "%"),
    "gpt": ("ppm", 1.0, "g/t"),
    "opt": ("ppm", OZ_PER_TON_TO_PPM, "oz/t"),
    "mgkg": ("ppm", 1.0, "mg/kg"),
    "ugg": ("ppm", 1.0, "ug/g"),
    "ugkg": ("ppb", 1.0, "ug/kg"),
}

#: Spellings rewritten to the tokens above BEFORE the header is split on
#: separators, because the separators ("/", "_", "%") are part of them.
_UNIT_SPELLINGS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<![a-z])g\s*[/_]\s*(?:t|mt|tonne|tonnes)(?![a-z])"), " gpt "),
    (re.compile(r"(?<![a-z])grams?\s*per\s*tonnes?(?![a-z])"), " gpt "),
    (re.compile(r"(?<![a-z])oz\s*[/_]?\s*(?:s?t|ton|tons)(?![a-z])"), " opt "),
    (re.compile(r"(?<![a-z])mg\s*/\s*kg(?![a-z])"), " mgkg "),
    (re.compile(r"(?<![a-z])[uµ]g\s*/\s*kg(?![a-z])"), " ugkg "),
    (re.compile(r"(?<![a-z])[uµ]g\s*/\s*g(?![a-z])"), " ugg "),
    (re.compile(r"wt\s*%|%|(?<![a-z])percent(?![a-z])"), " pct "),
)

#: Lab method / preparation codes that may follow an element with no unit
#: (``Au_FA``, ``Au-AA24``, ``Cu_ICP``). A qualifier that is none of these,
#: in a header with no unit, means the column is not an assay.
_METHOD_WORDS: frozenset[str] = frozenset({
    "fa", "aa", "aas", "icp", "ms", "oes", "aes", "icpms", "icpoes", "icpaes",
    "grav", "gr", "xrf", "inaa", "naa", "fire", "assay", "aqr", "ar", "4a",
    "me", "fus", "fusion", "lf", "pf", "met", "screen", "sfa", "cn", "leach",
    "bleg", "pxrf", "final", "best", "avg", "mean", "calc", "total", "tot",
})
_METHOD_CODE = re.compile(r"^[a-z]{1,4}\d{1,3}[a-z]?$")


@dataclass(frozen=True)
class AssaySpec:
    """What one assay column holds and how its values are stored."""

    element: str
    #: ``ppm`` / ``ppb`` / ``pct`` — the unit the value is STORED in.
    unit: str
    #: Multiply the source value by this to get the stored value.
    factor: float
    #: What the header said (``g/t``, ``oz/t``, ...), or None when assumed.
    source_unit: str | None
    #: Tokens beside the element and unit (lab method codes, "final", ...).
    qualifiers: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        """The ``commodity_assays`` key: ``Au_ppm``, ``U3O8_pct``."""
        return f"{self.element}_{self.unit}"

    @property
    def unit_assumed(self) -> bool:
        return self.source_unit is None

    @property
    def converted(self) -> bool:
        """The header named a unit that is not the stored one (g/t, oz/t, ...)."""
        return self.source_unit is not None and self.source_unit not in (
            "ppm", "ppb", "%",
        )


def _glued(token: str) -> tuple[str, str] | None:
    """``auppm`` / ``u3o8pct`` / ``cugpt`` -> (analyte, unit token)."""
    for unit in sorted(_UNITS, key=len, reverse=True):
        if token.endswith(unit) and token[: -len(unit)] in _ANALYTES:
            return _ANALYTES[token[: -len(unit)]], unit
    return None


def parse_assay_header(header: str | None) -> AssaySpec | None:
    """The assay a column header names, or None if it is not an assay column."""
    if header is None:
        return None
    text = str(header).strip().lower()
    if not text:
        return None
    for pattern, token in _UNIT_SPELLINGS:
        text = pattern.sub(token, text)
    tokens = [t for t in re.split(r"[^a-z0-9]+", text) if t]
    if not tokens:
        return None

    first, rest = tokens[0], tokens[1:]
    unit_tokens: list[str] = []
    if first in _ANALYTES:
        analyte = _ANALYTES[first]
    else:
        glued = _glued(first)
        if glued is None:
            return None
        analyte, unit_token = glued
        unit_tokens.append(unit_token)

    qualifiers: list[str] = []
    for token in rest:
        if token in _UNITS:
            unit_tokens.append(token)
        else:
            qualifiers.append(token)

    stored = {(_UNITS[u][0], _UNITS[u][1]) for u in unit_tokens}
    if len(stored) > 1:
        return None                      # two different units: not ours to pick

    if unit_tokens:
        unit, factor, source = _UNITS[unit_tokens[0]]
        return AssaySpec(analyte, unit, factor, source, tuple(qualifiers))

    # No unit named. Only an element-only header, or one whose extra words
    # are all lab method codes, is an assay - and its unit is an assumption.
    if len(analyte) == 1 and first == analyte.lower():
        return None                      # bare Y / S / U / W: too ambiguous
    if any(q not in _METHOD_WORDS and not _METHOD_CODE.match(q) for q in qualifiers):
        return None
    return AssaySpec(analyte, "ppm", 1.0, None, tuple(qualifiers))


def split_assay_key(key: str) -> tuple[str, str | None] | None:
    """``Au_ppm`` -> (``Au``, ``ppm``); a bare legacy key -> (element, None)."""
    spec = parse_assay_header(key)
    if spec is None:
        return None
    return spec.element, (None if spec.unit_assumed else spec.unit)


__all__ = [
    "OZ_PER_TON_TO_PPM",
    "AssaySpec",
    "parse_assay_header",
    "split_assay_key",
]
