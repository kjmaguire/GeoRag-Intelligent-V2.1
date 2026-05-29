"""1st-stage structural extractor — ADR-0007 PR-2.

Parses strike/dip and α/β acoustic-televiewer measurements out of existing
unstructured text and populates ``silver.structure`` rows. The existing
``silver_structure_derive`` (α/β → true_dip) and
``gold_structure_measurements_visual`` (stereonet x/y projection) cascade
fire automatically off the rows produced here.

Sources scanned per workspace:
  * ``silver.lithology_logs.notes`` (free-text geologist notes per interval,
    keyed by ``collar_id`` + interval depth)
  * ``silver.reports.sections_text`` (JSONB structural sections from
    NI 43-101 reports, no collar_id — captured for project-level context)

Notation patterns recognised:
  * ``045/72 SE``                  — strike / dip with quadrant
  * ``strike 215, dip 60 SW``      — comma-separated "strike X, dip Y"
  * ``strike: 045°, dip: 72° NE``  — colon-form with degree symbol
  * ``foliation: 080° / 35°``      — kind-prefixed "foliation T / P"
  * ``α=43° β=128°`` / ``alpha 43 beta 128``  — acoustic-televiewer
  * ``S1 foliation 045/72``, ``joint set 080/55``, ``fault zone 215/30 W``

Each match emits a row with workspace_id, collar_id (when derivable),
depth (when present in the source row), structure_type (foliation /
joint / fault / vein / contact / cleavage / bedding / shear / lineation
/ other — inferred from surrounding text), and either strike/dip OR
alpha/beta. The raw matched substring is preserved in ``notes`` for
audit. Idempotency: dedupe before insert on
(collar_id, depth, structure_type, alpha_angle, beta_angle).

NOTE: Do NOT add ``from __future__ import annotations`` — Dagster 1.13.
"""

import logging
import re
import uuid
from typing import Iterable, Optional

import psycopg2.extras
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.resources import PostgresResource


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structure-type vocabulary
# ---------------------------------------------------------------------------
# Must intersect the gold table CHECK in
# 2026_05_13_080002_create_gold_structure_measurements_visual.php. silver.
# structure has no CHECK, but downstream gold projection drops anything
# not on this list.
VALID_STRUCTURE_TYPES: tuple[str, ...] = (
    "fault", "shear", "fracture", "joint", "vein", "foliation", "cleavage",
    "bedding", "contact", "fold_axis", "lineation", "other",
)

# Keyword → canonical structure_type map. Order: longest / most specific
# matches first. The classifier scans the text window around each match
# (default ±60 chars) and picks the first hit.
_STRUCTURE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("fold axis", "fold_axis"),
    ("lineation", "lineation"),
    ("foliation", "foliation"),
    ("cleavage", "cleavage"),
    ("bedding", "bedding"),
    ("contact", "contact"),
    ("fracture", "fracture"),
    ("joint set", "joint"),
    ("joint", "joint"),
    ("fault zone", "fault"),
    ("fault", "fault"),
    ("shear zone", "shear"),
    ("shear", "shear"),
    ("vein", "vein"),
    ("s1", "foliation"),
    ("s2", "foliation"),
    ("s3", "foliation"),
)

_QUADRANT_TO_DEG: dict[str, float] = {
    "n": 0.0, "ne": 45.0, "e": 90.0, "se": 135.0,
    "s": 180.0, "sw": 225.0, "w": 270.0, "nw": 315.0,
}


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# 1) strike / dip with optional quadrant, e.g. "045/72 SE", "215/60SW".
_RE_STRIKE_DIP_QUAD = re.compile(
    r"\b(?P<strike>\d{1,3})\s*[/]\s*(?P<dip>\d{1,2})(?:\s*(?P<quad>NE|NW|SE|SW|N|E|S|W))?\b",
    re.IGNORECASE,
)

# 2) "strike 215, dip 60 SW" / "strike: 045°, dip: 72° NE"
_RE_STRIKE_DIP_LABELED = re.compile(
    r"strike[\s:]*?(?P<strike>\d{1,3})\s*°?\s*[,/;]?\s*"
    r"dip[\s:]*?(?P<dip>\d{1,2})\s*°?\s*"
    r"(?P<quad>NE|NW|SE|SW|N|E|S|W)?",
    re.IGNORECASE,
)

# 3) "foliation: 080° / 35°" / "joint 215 / 60" — kind-prefixed pair.
#    The kind word feeds the classifier instead of the surrounding window.
_RE_KIND_PREFIXED = re.compile(
    r"\b(?P<kind>foliation|cleavage|bedding|joint|fault|shear|vein|contact|fracture|lineation|fold[\s_]axis|s[123])\b"
    r"\s*[:#]?\s*"
    r"(?P<strike>\d{1,3})\s*°?\s*[/]\s*(?P<dip>\d{1,2})\s*°?"
    r"\s*(?P<quad>NE|NW|SE|SW|N|E|S|W)?",
    re.IGNORECASE,
)

# 4) α/β acoustic-televiewer: "α=43° β=128°", "alpha 43 beta 128",
#    "alpha:43 / beta:128"
_RE_ALPHA_BETA = re.compile(
    r"(?:α|alpha)\s*[:=]?\s*(?P<alpha>\d{1,3}(?:\.\d+)?)\s*°?"
    r".{0,30}?"
    r"(?:β|beta)\s*[:=]?\s*(?P<beta>\d{1,3}(?:\.\d+)?)\s*°?",
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

SELECT_LITHO_NOTES_SQL = """
SELECT
    l.collar_id::text AS collar_id,
    l.from_depth::float AS from_depth,
    l.to_depth::float AS to_depth,
    -- silver.lithology_logs has no dedicated `notes` column (verified
    -- 2026-05-25). lithology_description is the free-text geologist
    -- field; for many vendors it carries the structural notation the
    -- audit referenced as ``notes``. Treat it the same way.
    l.lithology_description AS notes
FROM silver.lithology_logs l
JOIN silver.collars c ON c.collar_id = l.collar_id
WHERE c.workspace_id = %(workspace_id)s::uuid
  AND (%(project_id)s::uuid IS NULL OR c.project_id = %(project_id)s::uuid)
  AND l.lithology_description IS NOT NULL
  AND length(l.lithology_description) > 4
"""

SELECT_REPORT_SECTIONS_SQL = """
SELECT
    r.report_id::text AS report_id,
    r.sections_text
FROM silver.reports r
JOIN silver.projects p ON p.project_id = r.project_id
WHERE p.workspace_id = %(workspace_id)s::uuid
  AND (%(project_id)s::uuid IS NULL OR r.project_id = %(project_id)s::uuid)
  AND r.sections_text IS NOT NULL
"""

# Idempotency dedupe key — drop any row already present with the same
# (collar_id, depth, structure_type, alpha_angle, beta_angle) signature.
# Workspace-scoped via the collar join in the SELECT.
EXISTING_KEYS_SQL = """
SELECT
    s.collar_id::text                AS collar_id,
    s.depth                          AS depth,
    s.structure_type                 AS structure_type,
    s.alpha_angle                    AS alpha_angle,
    s.beta_angle                     AS beta_angle,
    s.true_dip                       AS true_dip,
    s.true_dip_dir                   AS true_dip_dir
FROM silver.structure s
JOIN silver.collars c ON c.collar_id = s.collar_id
WHERE c.workspace_id = %(workspace_id)s::uuid
  AND (%(project_id)s::uuid IS NULL OR c.project_id = %(project_id)s::uuid)
"""

INSERT_STRUCTURE_SQL = """
INSERT INTO silver.structure (
    id, workspace_id, collar_id, depth, structure_type,
    alpha_angle, beta_angle, true_dip, true_dip_dir,
    notes
) VALUES (
    %(id)s::uuid, %(workspace_id)s::uuid, %(collar_id)s::uuid, %(depth)s, %(structure_type)s,
    %(alpha_angle)s, %(beta_angle)s, %(true_dip)s, %(true_dip_dir)s,
    %(notes)s
)
"""


# ---------------------------------------------------------------------------
# Classifier helpers
# ---------------------------------------------------------------------------

def _classify_structure_type(
    window_text: str,
    kind_hint: Optional[str] = None,
    *,
    pivot: Optional[int] = None,
) -> str:
    """Pick a canonical structure_type from a hint or surrounding text.

    When ``pivot`` is supplied, the closest-by-distance keyword in
    ``window_text`` wins (so "joint set 045/72 SE, fault zone 215/30 W"
    classifies the first match as joint and the second as fault). When
    ``pivot`` is None we fall back to first-keyword-in-window.
    """
    if kind_hint:
        norm = kind_hint.strip().lower().replace("_", " ")
        # S1 / S2 / S3 → foliation
        if norm in ("s1", "s2", "s3"):
            return "foliation"
        if norm.replace(" ", "_") in VALID_STRUCTURE_TYPES:
            return norm.replace(" ", "_")
        for keyword, canon in _STRUCTURE_KEYWORDS:
            if keyword in norm:
                return canon
    window = (window_text or "").lower()
    if not window:
        return "other"

    if pivot is None:
        for keyword, canon in _STRUCTURE_KEYWORDS:
            if keyword in window:
                return canon
        return "other"

    # Prefer the keyword closest to the pivot, weighting keywords that
    # appear BEFORE the match (the natural "joint set 045/72" reading)
    # by halving their distance. Each keyword's nearest occurrence on
    # either side is considered.
    best: tuple[float, str] | None = None
    for keyword, canon in _STRUCTURE_KEYWORDS:
        # Scan all occurrences of the keyword in the window.
        start = 0
        while True:
            idx = window.find(keyword, start)
            if idx == -1:
                break
            raw_dist = abs(idx - pivot)
            # Half-weight a keyword that precedes the match.
            dist = raw_dist * (0.5 if idx <= pivot else 1.0)
            if best is None or dist < best[0]:
                best = (dist, canon)
            start = idx + 1
    return best[1] if best is not None else "other"


def _resolve_dip_direction(
    strike_deg: Optional[float],
    dip_deg: Optional[float],
    quadrant: Optional[str],
) -> tuple[Optional[float], Optional[float]]:
    """Return (true_dip, true_dip_dir) when we have enough to project a planar
    measurement directly. Falls back to (None, None) when the dip direction
    can't be resolved (no quadrant).

    The right-hand rule says ``dip_direction = (strike + 90) mod 360`` when
    the dip is on the right-hand side of the strike. A quadrant hint
    disambiguates which side: we pick whichever of (strike+90) / (strike-90)
    falls in the named quadrant.
    """
    if strike_deg is None or dip_deg is None:
        return None, None
    if not quadrant:
        # Right-hand-rule strike implies dip_dir = strike + 90. Without a
        # quadrant hint we honour that convention rather than refusing.
        return float(dip_deg), (float(strike_deg) + 90.0) % 360.0

    quad_norm = quadrant.strip().lower()
    target = _QUADRANT_TO_DEG.get(quad_norm)
    if target is None:
        return float(dip_deg), (float(strike_deg) + 90.0) % 360.0

    # Two candidates from the strike line; pick whichever is closer to the
    # named quadrant centre (circular distance).
    cand_a = (float(strike_deg) + 90.0) % 360.0
    cand_b = (float(strike_deg) - 90.0) % 360.0

    def _circ_dist(a: float, b: float) -> float:
        d = abs(a - b) % 360.0
        return min(d, 360.0 - d)

    chosen = cand_a if _circ_dist(cand_a, target) <= _circ_dist(cand_b, target) else cand_b
    return float(dip_deg), chosen


# ---------------------------------------------------------------------------
# Extraction core — pure functions, easy to unit-test
# ---------------------------------------------------------------------------

def _window_around(text: str, start: int, end: int, radius: int = 40) -> str:
    """Slice ``text`` with `radius` chars on each side of [start, end)."""
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    return text[lo:hi]


def _window_with_pivot(
    text: str, start: int, end: int, radius: int = 40,
) -> tuple[str, int]:
    """Window slice + position of the match's midpoint within that slice."""
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    pivot = ((start + end) // 2) - lo
    return text[lo:hi], pivot


def extract_structure_candidates(
    *,
    text: str,
    collar_id: Optional[str],
    depth: Optional[float],
) -> list[dict]:
    """Scan ``text`` and return a list of candidate structure rows.

    Each row is a dict shaped for INSERT_STRUCTURE_SQL (minus workspace_id
    + id which the caller fills in). collar_id / depth pass through as
    provided — the report-sections branch passes None / None.

    Strategy:
      1. Kind-prefixed strike/dip first (highest precision)
      2. Labeled "strike X, dip Y" form
      3. α/β acoustic-televiewer pairs
      4. Bare "NNN/NN [QUAD]" strike/dip (lowest precision — needs a
         structural keyword nearby to avoid matching depths like "100/50 m")
    """
    out: list[dict] = []
    if not text:
        return out

    matched_spans: list[tuple[int, int]] = []

    def _overlaps(s: int, e: int) -> bool:
        for ms, me in matched_spans:
            if not (e <= ms or s >= me):
                return True
        return False

    # 1) kind-prefixed.
    for m in _RE_KIND_PREFIXED.finditer(text):
        s, e = m.span()
        try:
            strike = float(m.group("strike"))
            dip = float(m.group("dip"))
        except (TypeError, ValueError):
            continue
        if not (0 <= strike <= 360 and 0 <= dip <= 90):
            continue
        kind_hint = m.group("kind")
        quadrant = m.group("quad")
        stype = _classify_structure_type("", kind_hint=kind_hint)
        true_dip, true_dip_dir = _resolve_dip_direction(strike, dip, quadrant)
        out.append({
            "collar_id":    collar_id,
            "depth":        depth,
            "structure_type": stype,
            "alpha_angle":  None,
            "beta_angle":   None,
            "true_dip":     true_dip,
            "true_dip_dir": true_dip_dir,
            "notes":        m.group(0).strip(),
        })
        matched_spans.append((s, e))

    # 2) labeled strike/dip.
    for m in _RE_STRIKE_DIP_LABELED.finditer(text):
        s, e = m.span()
        if _overlaps(s, e):
            continue
        try:
            strike = float(m.group("strike"))
            dip = float(m.group("dip"))
        except (TypeError, ValueError):
            continue
        if not (0 <= strike <= 360 and 0 <= dip <= 90):
            continue
        quadrant = m.group("quad")
        window, pivot = _window_with_pivot(text, s, e)
        stype = _classify_structure_type(window, pivot=pivot)
        true_dip, true_dip_dir = _resolve_dip_direction(strike, dip, quadrant)
        out.append({
            "collar_id":    collar_id,
            "depth":        depth,
            "structure_type": stype,
            "alpha_angle":  None,
            "beta_angle":   None,
            "true_dip":     true_dip,
            "true_dip_dir": true_dip_dir,
            "notes":        m.group(0).strip(),
        })
        matched_spans.append((s, e))

    # 3) α / β acoustic-televiewer.
    for m in _RE_ALPHA_BETA.finditer(text):
        s, e = m.span()
        if _overlaps(s, e):
            continue
        try:
            alpha = float(m.group("alpha"))
            beta = float(m.group("beta"))
        except (TypeError, ValueError):
            continue
        if not (0 <= alpha <= 90 and 0 <= beta <= 360):
            continue
        window, pivot = _window_with_pivot(text, s, e)
        stype = _classify_structure_type(window, pivot=pivot)
        out.append({
            "collar_id":    collar_id,
            "depth":        depth,
            "structure_type": stype,
            "alpha_angle":  alpha,
            "beta_angle":   beta,
            "true_dip":     None,
            "true_dip_dir": None,
            "notes":        m.group(0).strip(),
        })
        matched_spans.append((s, e))

    # 4) bare strike/dip + quadrant — only accept when a structural keyword
    # is within the ±60-char window, otherwise we'd match interval notations
    # like "100/50 m" or RQD values.
    for m in _RE_STRIKE_DIP_QUAD.finditer(text):
        s, e = m.span()
        if _overlaps(s, e):
            continue
        try:
            strike = float(m.group("strike"))
            dip = float(m.group("dip"))
        except (TypeError, ValueError):
            continue
        # Strike values > 360 OR dip values > 90 are not strike/dip.
        if not (0 <= strike <= 360 and 0 <= dip <= 90):
            continue
        window, pivot = _window_with_pivot(text, s, e)
        stype = _classify_structure_type(window, pivot=pivot)
        if stype == "other":
            # No structural keyword nearby — skip to avoid coining bogus
            # measurements from interval lengths or grade ratios.
            continue
        quadrant = m.group("quad")
        true_dip, true_dip_dir = _resolve_dip_direction(strike, dip, quadrant)
        out.append({
            "collar_id":    collar_id,
            "depth":        depth,
            "structure_type": stype,
            "alpha_angle":  None,
            "beta_angle":   None,
            "true_dip":     true_dip,
            "true_dip_dir": true_dip_dir,
            "notes":        m.group(0).strip(),
        })
        matched_spans.append((s, e))

    return out


def _dedupe_candidates(
    candidates: Iterable[dict],
    *,
    existing_keys: set[tuple],
) -> list[dict]:
    """Drop candidates that collide with existing rows OR with each other
    on (collar_id, depth, structure_type, alpha_angle, beta_angle).
    """
    seen: set[tuple] = set(existing_keys)
    out: list[dict] = []
    for c in candidates:
        key = (
            c.get("collar_id"),
            c.get("depth"),
            c.get("structure_type"),
            c.get("alpha_angle"),
            c.get("beta_angle"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _flatten_sections_text(value) -> str:
    """Best-effort flattener for ``silver.reports.sections_text`` JSONB.

    The column shape varies across NI 43-101 ingest paths — sometimes a
    list of {title, text} dicts, sometimes a flat dict {section_title:
    body}, sometimes already a string. We concatenate all string leaves
    so the regex pass sees them.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_flatten_sections_text(v) for v in value)
    if isinstance(value, dict):
        return "\n".join(_flatten_sections_text(v) for v in value.values())
    return str(value)


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

class SilverStructurePopulateConfig(Config):
    """Runtime configuration for the silver_structure_populate asset."""

    workspace_id: str = "a0000000-0000-0000-0000-000000000001"
    project_id: str = ""  # empty → all projects in the workspace


@asset(
    group_name="silver",
    description=(
        "1st-stage structural extractor (ADR-0007 PR-2). Lifts strike/dip "
        "+ α/β orientation measurements from silver.lithology_logs.notes "
        "and silver.reports.sections_text into silver.structure. The "
        "existing silver_structure_derive + gold_structure_measurements_visual "
        "cascade fires automatically off the rows produced here. "
        "SCHEDULED (silver_chat_cards_backfill_schedule) — runs every 30 "
        "minutes so new projects auto-populate structural rows without "
        "manual trigger."
    ),
)
def silver_structure_populate(
    context: AssetExecutionContext,
    config: SilverStructurePopulateConfig,
    postgres: PostgresResource,
) -> MaterializeResult:
    """Populate ``silver.structure`` from free-text geological notes."""

    project_id_val = config.project_id if config.project_id else None

    candidates: list[dict] = []
    litho_rows_scanned = 0
    report_rows_scanned = 0

    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 1) lithology_log notes — collar_id + depth carry through.
            cur.execute(
                SELECT_LITHO_NOTES_SQL,
                {"workspace_id": config.workspace_id, "project_id": project_id_val},
            )
            for row in cur.fetchall():
                litho_rows_scanned += 1
                depth_anchor = None
                if row["from_depth"] is not None:
                    # Anchor each match to the interval start so downstream
                    # consumers can plot at-depth. to_depth is intentionally
                    # not used — most notes describe an observation at a
                    # depth horizon, not an averaged interval.
                    depth_anchor = float(row["from_depth"])
                hits = extract_structure_candidates(
                    text=row["notes"] or "",
                    collar_id=row["collar_id"],
                    depth=depth_anchor,
                )
                candidates.extend(hits)

            # 2) report sections_text — no collar_id, depth is None.
            cur.execute(
                SELECT_REPORT_SECTIONS_SQL,
                {"workspace_id": config.workspace_id, "project_id": project_id_val},
            )
            for row in cur.fetchall():
                report_rows_scanned += 1
                flat = _flatten_sections_text(row["sections_text"])
                if not flat:
                    continue
                # Report-level extractions have no collar anchor. silver.
                # structure requires collar_id NOT NULL so these are NOT
                # inserted in this pass — we still extract them for the
                # metric so the asset surfaces what's locked behind the
                # missing collar pinning. A future iteration can plug
                # NER (hole_id from sections_text → collar_id) here.
                hits = extract_structure_candidates(
                    text=flat,
                    collar_id=None,
                    depth=None,
                )
                candidates.extend(hits)

            # Existing-row signature set for idempotent dedupe.
            cur.execute(
                EXISTING_KEYS_SQL,
                {"workspace_id": config.workspace_id, "project_id": project_id_val},
            )
            existing_keys = set()
            for row in cur.fetchall():
                existing_keys.add((
                    str(row["collar_id"]) if row["collar_id"] else None,
                    float(row["depth"]) if row["depth"] is not None else None,
                    row["structure_type"],
                    float(row["alpha_angle"]) if row["alpha_angle"] is not None else None,
                    float(row["beta_angle"]) if row["beta_angle"] is not None else None,
                ))

        # Drop candidates without collar_id (silver.structure FK NOT NULL).
        with_collar = [c for c in candidates if c.get("collar_id") is not None]
        dropped_no_collar = len(candidates) - len(with_collar)

        deduped = _dedupe_candidates(with_collar, existing_keys=existing_keys)
        skipped_existing = len(with_collar) - len(deduped)

        insert_rows: list[dict] = []
        for c in deduped:
            insert_rows.append({
                "id":             str(uuid.uuid4()),
                "workspace_id":   config.workspace_id,
                "collar_id":      c["collar_id"],
                "depth":          c["depth"] if c["depth"] is not None else 0.0,
                "structure_type": c["structure_type"],
                "alpha_angle":    c["alpha_angle"],
                "beta_angle":     c["beta_angle"],
                "true_dip":       c["true_dip"],
                "true_dip_dir":   c["true_dip_dir"],
                "notes":          (c["notes"] or "")[:500],
            })

        with conn.cursor() as cur:
            if insert_rows:
                psycopg2.extras.execute_batch(
                    cur, INSERT_STRUCTURE_SQL, insert_rows, page_size=200,
                )
        conn.commit()

    inserted = len(insert_rows)
    context.log.info(
        "silver_structure_populate: workspace=%s project=%s "
        "litho_rows=%d report_rows=%d candidates=%d dropped_no_collar=%d "
        "skipped_existing=%d inserted=%d",
        config.workspace_id, project_id_val or "(all)",
        litho_rows_scanned, report_rows_scanned, len(candidates),
        dropped_no_collar, skipped_existing, inserted,
    )

    return MaterializeResult(
        metadata={
            "workspace_id":         MetadataValue.text(config.workspace_id),
            "project_id":           MetadataValue.text(project_id_val or ""),
            "litho_rows_scanned":   MetadataValue.int(litho_rows_scanned),
            "report_rows_scanned":  MetadataValue.int(report_rows_scanned),
            "candidates_total":     MetadataValue.int(len(candidates)),
            "dropped_no_collar":    MetadataValue.int(dropped_no_collar),
            "skipped_existing":     MetadataValue.int(skipped_existing),
            "inserted":             MetadataValue.int(inserted),
        }
    )


__all__ = [
    "VALID_STRUCTURE_TYPES",
    "SilverStructurePopulateConfig",
    "silver_structure_populate",
    "extract_structure_candidates",
    "_classify_structure_type",
    "_resolve_dip_direction",
    "_dedupe_candidates",
    "_flatten_sections_text",
]
