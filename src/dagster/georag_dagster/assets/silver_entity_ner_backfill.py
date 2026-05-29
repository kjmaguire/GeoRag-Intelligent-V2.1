"""Entity-NER backfill — ADR-0007 PR-3.

Single Dagster asset that lifts contractor / geologist (QP) / lab names and
hole-IDs out of ``silver.reports.sections_text`` + ``silver.reports.authors``
and uses them to backfill downstream silver columns that the 2026-05-25 audit
found 0% populated:

* ``silver.campaigns.contractor`` / ``silver.geophysics_surveys.contractor``
* ``silver.campaigns.geologist`` / ``silver.collars.geologist``
* ``silver.assays_v2.lab_name``
* ``silver.reports.qp_name`` (text[])

Plus a fourth NER pass for hole-IDs that re-anchors PR-2's report-section
structural rows. ``silver_structure_populate`` correctly drops structural
notations parsed from ``silver.reports.sections_text`` because the report
text covers a whole project and has no per-row collar id. This pass walks
the same text, finds structural notations within ±300 chars of a hole-ID
mention, fuzzy-matches the mention against ``silver.collars.hole_id_canonical``
(pg_trgm similarity > 0.8), and re-inserts the structural rows anchored to
the matched collar_id. Idempotent on the same dedupe key.

Neo4j side-effect: each unique QP name is MERGE'd as a ``:QP`` node per the
§04f knowledge-graph addendum, with a ``(:Report)-[:AUTHORED_BY]->(:QP)``
edge per source report.

Implementation notes:
  * Pure regex / curated-allowlist NER. spaCy is not in the dagster
    container; the cost / install footprint of ``en_core_web_sm`` is not
    justified when the geologist / contractor / lab vocabularies are
    small and well-known. The post-filter regex below is what the
    audit-prompt called out as the "broader than the allowlist" rule.
  * All writes guarded with ``UPDATE ... WHERE column IS NULL`` so a
    re-run is idempotent. The structural re-anchor pass dedupes on the
    same ``(collar_id, depth, structure_type, alpha_angle, beta_angle)``
    key PR-2 uses.
  * Workspace-scoped throughout.

NOTE: Do NOT add ``from __future__ import annotations`` — Dagster 1.13.
"""

import logging
import re
import uuid
from collections import Counter
from typing import Iterable, Optional

import psycopg2.extras
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets.silver_structure_populate import (
    _flatten_sections_text,
    extract_structure_candidates,
    silver_structure_populate,
)
from georag_dagster.parsers._hole_id import canonicalize as canonicalize_hole_id
from georag_dagster.resources import Neo4jResource, PostgresResource


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Curated vocabularies — small, well-known. Order: longer / more specific
# first so "Major Drilling" wins over "Major" if "Major" ever gets added.
# ---------------------------------------------------------------------------

# Known drill contractors (Canadian-mining-focused, broad enough for the
# Athabasca / Saskatchewan corpus the 2026-05-25 audit was scoped to). Casing
# in this list is the canonical writeback form; matching is case-insensitive.
CONTRACTOR_ALLOWLIST: tuple[str, ...] = (
    "Boart Longyear",
    "Major Drilling",
    "Foraco",
    "Energold",
    "Cementation",
    "Hy-Tech Drilling",
    "Cabo Drilling",
    "Hardrock Drills",
    "Orbit Garant",
    "Capital Drilling",
    "Layne Christensen",
    "Discovery Drilling",
)

# Known assay / analytical labs (Canadian + global). Matching is case-
# insensitive; we accept the trailing words "Geochemistry" / "Laboratories"
# / "Labs" / "Analytical" / "Minerals" / "USA" / "Canada" etc. as part of
# the same vendor.
LAB_ALLOWLIST: tuple[str, ...] = (
    "ALS Geochemistry",
    "ALS Chemex",
    "ALS Minerals",
    "ALS",
    "SGS Canada",
    "SGS Minerals",
    "SGS",
    "Bureau Veritas",
    "Activation Laboratories",
    "Actlabs",
    "AGAT Laboratories",
    "AGAT",
    "MS Analytical",
    "Inspectorate",
    "Loring Laboratories",
    "Loring",
    "TSL Laboratories",
    "Saskatchewan Research Council",
    "SRC Geoanalytical",
    "SRC",
)


# ---------------------------------------------------------------------------
# Regex — contractor / lab / QP / hole-ID
# ---------------------------------------------------------------------------

# Broader "X did the drilling" pattern — catches contractors not in the
# allowlist. Captures up to ~6 capitalised words as the ORG name.
_RE_CONTRACTOR_VERB = re.compile(
    r"\b(?P<org>(?:[A-Z][A-Za-z&.\-]+(?:\s+[A-Z][A-Za-z&.\-]+){0,5}))"
    r"\s+(?:was|has been|were|have been)?\s*"
    r"(?:contracted to|performed|undertook|completed|carried out|conducted)\s+"
    r"(?:the\s+)?(?:drilling|drill\s*program|drill\s*campaign|"
    r"diamond\s+drilling|RC\s+drilling|survey|geophysical\s+survey)",
    re.IGNORECASE,
)

_RE_CONTRACTOR_BY = re.compile(
    r"(?:drilling|drill\s*program|drill\s*campaign|survey)\s+"
    r"(?:was|were)?\s*"
    r"(?:undertaken|performed|completed|carried out|conducted)\s+"
    r"by\s+(?P<org>(?:[A-Z][A-Za-z&.\-]+(?:\s+[A-Z][A-Za-z&.\-]+){0,5}))",
    re.IGNORECASE,
)

# QP / geologist person pattern. Catches:
#   "John Smith, P.Geo, Senior Project Geologist"
#   "Jane Doe, M.Sc., P.Eng., Qualified Person"
#   "logged by Sam O'Connor"
#   "supervised by Dr. Alice Brown"
#
# Name halves are intentionally case-SENSITIVE: re.IGNORECASE on the whole
# pattern let lower-case sentence fragments like "estimates were prepared
# by the Qualified Persons listed" satisfy the [A-Z][a-z]+ token shape
# (the live workspace's first run surfaced 77 candidates of which only 8
# survived the downstream filter). Inline `(?i:...)` keeps the credential
# alternatives and the verb prefix case-insensitive so "p.geo", "Logged
# By", etc. still match. The per-token char class is broadened to
# [A-Za-z'\-]+ so mixed-case real names ("OConnor", "MacDonald") still
# match without IGNORECASE.
_RE_PERSON_TITLED = re.compile(
    r"\b(?P<name>(?:Dr\.?\s+)?[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){1,3})"
    r"\s*,?\s*"
    r"(?P<credential>(?i:M\.?\s*Sc\.?|Ph\.?\s*D\.?|B\.?\s*Sc\.?|"
    r"P\.?\s*Geo\.?|P\.?\s*Eng\.?|MAusIMM|MAIG|FAusIMM|"
    r"Qualified Person|QP|"
    r"(?:project|exploration|senior|chief|consulting)?\s*geologist)"
    r"\b)",
)

_RE_PERSON_VERB = re.compile(
    r"(?i:\b(?:logged|supervised|reviewed|prepared|signed)\s+by\s+"
    r"(?:Dr\.?\s+|Mr\.?\s+|Ms\.?\s+|Mrs\.?\s+)?)"
    r"(?P<name>[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){1,3})\b",
)


# Sentence-fragment tokens that the QP regex can sweep into the name when
# the credential pattern matches a nearby phrase. Lower-cased single-token
# entries; the filter checks each whitespace-split token against this set.
# Includes verbs/participles, determiners/pronouns, common prepositions
# and copulas, and report-section header words.
_QP_NAME_STOPWORDS: frozenset[str] = frozenset({
    # Verbs / participles (multiple tenses).
    "prepared", "prepares", "preparing",
    "listed", "lists", "listing",
    "estimated", "estimates", "estimating",
    "completed", "completes", "completing",
    "reviewed", "reviews", "reviewing",
    "supervised", "supervises", "supervising",
    "logged", "logs", "logging",
    "signed", "signs", "signing",
    "conducted", "conducts", "conducting",
    "undertaken", "undertook", "undertakes", "undertaking",
    "performed", "performs", "performing",
    "authored", "authors", "authoring",
    "compiled", "compiles", "compiling",
    "drafted", "drafts", "drafting",
    "approved", "approves", "approving",
    "submitted", "submits", "submitting",
    "verified", "verifies", "verifying",
    "validated", "validates", "validating",
    "appointed", "appoints", "appointing",
    # Determiners / pronouns.
    "the", "this", "that", "these", "those", "their", "his", "her",
    # Prepositions / copulas that bridge verb-fragment captures.
    "by", "of", "in", "on", "at", "for", "with", "from",
    "was", "were", "is", "are",
    # Report-section header words. ("Qualified Person" the credential is
    # fine; capturing it as a NAME is what we reject.)
    "qualified", "persons", "person",
    "contents", "appendix", "references", "acknowledgements",
    "section", "introduction", "summary", "abstract",
    # Job-title fragments that double as credential modifiers.
    "senior", "junior", "chief", "consulting", "project", "exploration",
    "geologist", "engineer",
})


def _strip_stopword_edges(name: str) -> str:
    """Trim leading/trailing stopword tokens.

    Handles the greedy-match case where the regex pulled in extra tokens
    on either side of a real name (e.g. "Qualified Persons John Smith" ->
    "John Smith"). Interior stopwords are NOT trimmed — they signal a
    sentence fragment and the caller should reject the whole capture.
    """
    tokens = name.split()
    while tokens and tokens[0].lower() in _QP_NAME_STOPWORDS:
        tokens.pop(0)
    while tokens and tokens[-1].lower() in _QP_NAME_STOPWORDS:
        tokens.pop()
    return " ".join(tokens)


def _is_valid_qp_name(name: str) -> bool:
    """Filter out sentence fragments masquerading as names.

    Rejects:
      * empty / shorter than 5 chars
      * single-token (no whitespace) — "Smith" alone is too generic
      * any individual token is in _QP_NAME_STOPWORDS — catches
        residual "estimates Were Prepared", "the Authors", etc.
    """
    if not name or len(name) < 5 or " " not in name:
        return False
    tokens = name.lower().split()
    if any(tok in _QP_NAME_STOPWORDS for tok in tokens):
        return False
    return True

# Lab "analyzed at / by X"
_RE_LAB_VERB = re.compile(
    r"\b(?:analy[sz]ed|assayed|certified|prepared)\s+(?:at|by)\s+"
    r"(?P<lab>(?:[A-Z][A-Za-z&.\-]+(?:\s+[A-Z][A-Za-z&.\-]+){0,4}))",
    re.IGNORECASE,
)

# Hole-IDs — reused from app.agent.viz_builder per CLAUDE.md / memory
# `project_hole_id_extraction_2026_05_21`. The two patterns cover:
#   1) Letter-prefixed (PLS-22-08, DH-2547, XLS-24-09, CAM-12-001)
#   2) Numeric-only behind a context word ("hole 36-1085", "ddh 99-001")
_HOLE_ID_RE = re.compile(
    r"\b([A-Z]{2,6}\d{0,4}-\d{1,5}(?:-\d{1,5})?)\b",
    re.IGNORECASE,
)
_NUMERIC_HOLE_ID_RE = re.compile(
    r"\b(?:hole|drill\s*hole|ddh|dh|drill)s?\s+(\d{1,4}-\d{1,5}(?:-\d{1,5})?)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Pure-function extractors (no DB, no Dagster) — easy to unit-test.
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    """Whitespace + casing-normalised key for de-duping inside one report."""
    return " ".join((s or "").split()).strip().lower()


def extract_contractors(text: str) -> list[str]:
    """Return canonical contractor names mentioned in ``text``.

    Combines the allowlist (case-insensitive substring) with two broader
    verb patterns. Order preserved; duplicates removed.
    """
    if not text:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    text_lc = text.lower()

    for allow in CONTRACTOR_ALLOWLIST:
        if allow.lower() in text_lc:
            k = _norm(allow)
            if k not in seen:
                seen.add(k)
                ordered.append(allow)

    for m in _RE_CONTRACTOR_VERB.finditer(text):
        cand = (m.group("org") or "").strip(" .,;-")
        if not cand or len(cand) < 4:
            continue
        # Skip if the verb pattern matched a leading sentence start that
        # captured non-ORG words (e.g. "The drilling was undertaken by ...").
        if cand.lower() in {"the", "this", "drilling", "drill program"}:
            continue
        k = _norm(cand)
        if k not in seen:
            seen.add(k)
            ordered.append(cand)

    for m in _RE_CONTRACTOR_BY.finditer(text):
        cand = (m.group("org") or "").strip(" .,;-")
        if not cand or len(cand) < 4:
            continue
        k = _norm(cand)
        if k not in seen:
            seen.add(k)
            ordered.append(cand)

    return ordered


def extract_qps(text: str) -> list[str]:
    """Return PERSON names mentioned alongside a QP / geologist credential.

    Two patterns:
      1. ``Name, P.Geo`` / ``Name, Qualified Person`` (titled form)
      2. ``logged by Name`` / ``supervised by Name`` (verb form)
    """
    if not text:
        return []
    seen: set[str] = set()
    ordered: list[str] = []

    for m in _RE_PERSON_TITLED.finditer(text):
        name = (m.group("name") or "").strip()
        if not name:
            continue
        # Drop leading honorifics ("Dr. ").
        name = re.sub(r"^(?:Dr\.?|Mr\.?|Mrs\.?|Ms\.?)\s+", "", name).strip()
        name = _strip_stopword_edges(name)
        if not _is_valid_qp_name(name):
            continue
        k = _norm(name)
        if k and k not in seen:
            seen.add(k)
            ordered.append(name)

    for m in _RE_PERSON_VERB.finditer(text):
        name = (m.group("name") or "").strip()
        name = _strip_stopword_edges(name)
        if not _is_valid_qp_name(name):
            continue
        k = _norm(name)
        if k and k not in seen:
            seen.add(k)
            ordered.append(name)

    return ordered


def extract_labs(text: str) -> list[str]:
    """Return canonical lab names mentioned in ``text``."""
    if not text:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    text_lc = text.lower()

    for allow in LAB_ALLOWLIST:
        if allow.lower() in text_lc:
            k = _norm(allow)
            if k not in seen:
                seen.add(k)
                ordered.append(allow)

    for m in _RE_LAB_VERB.finditer(text):
        cand = (m.group("lab") or "").strip(" .,;-")
        if not cand or len(cand) < 3:
            continue
        # Reject sentence-start common words.
        if cand.lower() in {"the", "this", "all", "drilling"}:
            continue
        k = _norm(cand)
        if k not in seen:
            seen.add(k)
            ordered.append(cand)

    return ordered


def extract_hole_ids(text: str) -> list[tuple[str, int, int]]:
    """Return (hole_id_upper, start, end) tuples for every match in ``text``.

    Combines the lettered and numeric-context patterns from
    ``app.agent.viz_builder``. Positions are character offsets within
    ``text`` — the re-anchor pass uses them to find structural notations
    in the surrounding ±300-char window.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[tuple[str, int, int]] = []
    for m in _HOLE_ID_RE.finditer(text):
        hid = m.group(1).upper()
        if hid not in seen:
            seen.add(hid)
            out.append((hid, m.start(1), m.end(1)))
    for m in _NUMERIC_HOLE_ID_RE.finditer(text):
        hid = m.group(1).upper()
        if hid not in seen:
            seen.add(hid)
            out.append((hid, m.start(1), m.end(1)))
    return out


def majority(values: Iterable[Optional[str]]) -> Optional[str]:
    """Return the most-frequent non-empty string in ``values``, or None."""
    counter: Counter[str] = Counter()
    for v in values:
        if v:
            counter[v] += 1
    if not counter:
        return None
    return counter.most_common(1)[0][0]


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

SELECT_REPORTS_SQL = """
SELECT
    r.report_id::text         AS report_id,
    r.project_id::text        AS project_id,
    r.sections_text           AS sections_text,
    r.authors                 AS authors,
    r.qp_name                 AS qp_name
FROM silver.reports r
JOIN silver.projects p ON p.project_id = r.project_id
WHERE p.workspace_id = %(workspace_id)s::uuid
  AND (%(project_id)s::uuid IS NULL OR r.project_id = %(project_id)s::uuid)
"""

UPDATE_REPORT_QP_SQL = """
UPDATE silver.reports
SET qp_name = %(qp_name)s
WHERE report_id = %(report_id)s::uuid
  AND (qp_name IS NULL OR cardinality(qp_name) = 0)
"""

UPDATE_CAMPAIGN_CONTRACTOR_SQL = """
UPDATE silver.campaigns
SET contractor = %(contractor)s
WHERE workspace_id = %(workspace_id)s::uuid
  AND project_id = %(project_id)s::uuid
  AND contractor IS NULL
"""

UPDATE_CAMPAIGN_GEOLOGIST_SQL = """
UPDATE silver.campaigns
SET geologist = %(geologist)s
WHERE workspace_id = %(workspace_id)s::uuid
  AND project_id = %(project_id)s::uuid
  AND geologist IS NULL
"""

UPDATE_GEOPHYS_CONTRACTOR_SQL = """
UPDATE silver.geophysics_surveys
SET contractor = %(contractor)s
WHERE workspace_id = %(workspace_id)s::uuid
  AND project_id = %(project_id)s::uuid
  AND contractor IS NULL
"""

UPDATE_COLLAR_GEOLOGIST_SQL = """
UPDATE silver.collars
SET geologist = %(geologist)s
WHERE workspace_id = %(workspace_id)s::uuid
  AND project_id = %(project_id)s::uuid
  AND hole_id_canonical = %(hole_id_canonical)s
  AND geologist IS NULL
"""

UPDATE_ASSAY_LAB_SQL = """
UPDATE silver.assays_v2 a
SET lab_name = %(lab_name)s
FROM silver.collars c
WHERE a.collar_id = c.collar_id
  AND c.workspace_id = %(workspace_id)s::uuid
  AND c.project_id = %(project_id)s::uuid
  AND a.lab_name IS NULL
"""

# Pg_trgm fuzzy match. The collar's hole_id_canonical is upper-cased on
# write (per parsers/csv_collar.py) so we compare against the upper-case
# mention. similarity threshold 0.8 to match the spec.
FUZZY_MATCH_HOLE_SQL = """
SELECT collar_id::text, hole_id_canonical
FROM silver.collars
WHERE workspace_id = %(workspace_id)s::uuid
  AND hole_id_canonical IS NOT NULL
  AND similarity(hole_id_canonical, %(hole_id)s) > 0.8
ORDER BY similarity(hole_id_canonical, %(hole_id)s) DESC
LIMIT 1
"""

# When the NER pass extracts a hole-ID from report text, also keep
# silver.collars.hole_id_canonical in sync — reuses the CSV parser's
# canonicalize() rule so a legacy NULL-canonical collar whose hole_id
# matches the extracted form gets repaired here. WHERE filters keep the
# write idempotent (only fills NULL) and workspace-scoped.
UPDATE_COLLAR_CANONICAL_SQL = """
UPDATE silver.collars
SET hole_id_canonical = %(hole_id_canonical)s
WHERE workspace_id = %(workspace_id)s::uuid
  AND project_id = %(project_id)s::uuid
  AND hole_id_canonical IS NULL
  AND regexp_replace(upper(hole_id), '[ \\-_./]+', '', 'g') = %(hole_id_canonical)s
"""

EXISTING_STRUCTURE_KEYS_SQL = """
SELECT
    s.collar_id::text AS collar_id,
    s.depth           AS depth,
    s.structure_type  AS structure_type,
    s.alpha_angle     AS alpha_angle,
    s.beta_angle      AS beta_angle
FROM silver.structure s
JOIN silver.collars c ON c.collar_id = s.collar_id
WHERE c.workspace_id = %(workspace_id)s::uuid
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
# Neo4j Cypher
# ---------------------------------------------------------------------------

MERGE_QP_CYPHER = """
UNWIND $qps AS qp
MERGE (q:QP {name: qp.name, workspace_id: qp.workspace_id})
  ON CREATE SET
    q.first_seen_report = qp.report_id,
    q.created_at        = datetime()
  ON MATCH SET
    q.last_seen_report  = qp.report_id,
    q.last_updated      = datetime()
WITH q, qp
// Report nodes are constrained on report_id alone (see index_neo4j.py
// and §04f). MERGE only on that key so we don't trip the uniqueness
// constraint when the workspace_id property happens to be different
// (e.g. older Report nodes written without workspace_id at all).
MERGE (r:Report {report_id: qp.report_id})
  ON CREATE SET r.workspace_id = qp.workspace_id
  ON MATCH SET  r.workspace_id = coalesce(r.workspace_id, qp.workspace_id)
MERGE (r)-[:AUTHORED_BY]->(q)
"""


# ---------------------------------------------------------------------------
# Re-anchor pass — feed dropped report-section structural rows back through
# the hole-ID NER and re-insert them with the matched collar_id.
# ---------------------------------------------------------------------------

def reanchor_candidates(
    *,
    text: str,
    hole_id_to_collar: dict[str, str],
    radius: int = 300,
) -> list[dict]:
    """Pair each structural-notation match with the nearest hole-ID mention.

    Walks ``text`` once, finds every hole-ID with its char-offset, then for
    each PR-2 structural candidate (no collar_id) picks the closest hole-ID
    within ``radius`` characters and re-anchors the row to that collar.

    Returns rows already shaped for ``INSERT_STRUCTURE_SQL`` (minus the
    workspace_id / id which the caller fills in).
    """
    if not text:
        return []
    hole_mentions = extract_hole_ids(text)
    if not hole_mentions:
        return []

    # PR-2 candidates for the same text — already classified, true_dip
    # computed, etc. Each carries collar_id=None.
    pr2_candidates = extract_structure_candidates(
        text=text, collar_id=None, depth=None,
    )
    if not pr2_candidates:
        return []

    # We need each PR-2 candidate's position back. The simplest robust
    # approach: re-scan text for the candidate's ``notes`` substring (the
    # raw match) and use that as the position pivot. This handles the
    # common case of distinct notations cleanly; identical substrings in
    # multiple places resolve to the first hit which is the same row PR-2
    # surfaced.
    out: list[dict] = []
    for cand in pr2_candidates:
        notes = (cand.get("notes") or "").strip()
        if not notes:
            continue
        idx = text.find(notes)
        if idx == -1:
            continue
        cand_pos = idx + len(notes) // 2

        # Pick the closest hole-ID within ``radius``.
        best: tuple[float, str] | None = None
        for hid, hs, he in hole_mentions:
            hid_pos = (hs + he) // 2
            dist = abs(hid_pos - cand_pos)
            if dist > radius:
                continue
            collar = hole_id_to_collar.get(hid)
            if collar is None:
                continue
            if best is None or dist < best[0]:
                best = (dist, collar)

        if best is None:
            continue

        out.append({
            "collar_id":      best[1],
            "depth":          cand.get("depth"),
            "structure_type": cand["structure_type"],
            "alpha_angle":    cand.get("alpha_angle"),
            "beta_angle":     cand.get("beta_angle"),
            "true_dip":       cand.get("true_dip"),
            "true_dip_dir":   cand.get("true_dip_dir"),
            "notes":          notes,
        })
    return out


# ---------------------------------------------------------------------------
# Asset config
# ---------------------------------------------------------------------------

class SilverEntityNerBackfillConfig(Config):
    """Runtime configuration for the silver_entity_ner_backfill asset."""

    workspace_id: str = "a0000000-0000-0000-0000-000000000001"
    project_id: str = ""  # empty → all projects in the workspace


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="silver",
    deps=[silver_structure_populate],
    description=(
        "Entity-NER backfill (ADR-0007 PR-3). Lifts contractor / geologist "
        "(QP) / lab names + hole-IDs out of silver.reports.sections_text + "
        "silver.reports.authors and backfills silver.campaigns / "
        "silver.collars / silver.assays_v2 / silver.geophysics_surveys + "
        "silver.reports.qp_name. Hole-ID pass also re-anchors PR-2's "
        "dropped report-section structural rows and repairs legacy NULL "
        "silver.collars.hole_id_canonical for matched holes. QP nodes "
        "pushed to Neo4j. "
        "SCHEDULED (silver_chat_cards_backfill_schedule) — runs every 30 "
        "minutes alongside silver_structure_populate so new projects "
        "auto-populate the chat-card data without manual trigger."
    ),
)
def silver_entity_ner_backfill(
    context: AssetExecutionContext,
    config: SilverEntityNerBackfillConfig,
    postgres: PostgresResource,
    neo4j: Neo4jResource,
) -> MaterializeResult:
    """Backfill 0%-populated silver columns from report text + authors."""

    project_id_val = config.project_id if config.project_id else None
    workspace_id = config.workspace_id

    # Per-project aggregations.
    project_contractors: dict[str, list[str]] = {}
    project_geologists: dict[str, list[str]] = {}
    project_labs: dict[str, list[str]] = {}
    # Hole-level mappings ((project_id, hole_id_canonical) → geologist).
    collar_geologist_hints: dict[tuple[str, str], str] = {}
    # QP + report-id pairs for Neo4j MERGE.
    qp_report_pairs: list[dict] = []
    # Per-report qp_name array updates.
    report_qp_updates: list[dict] = []

    # Re-anchor structural rows: per-project bag of (text, collar_lookup).
    reanchor_inputs: list[tuple[str, dict[str, str]]] = []

    reports_scanned = 0
    contractors_seen_total = 0
    geologists_seen_total = 0
    labs_seen_total = 0
    hole_ids_seen_total = 0
    # (project_id, hole_id_canonical) tuples seen during NER. Drives the
    # canonical-backfill repair pass at the end of pass 2.
    ner_canonical_hits: set[tuple[str, str]] = set()

    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                SELECT_REPORTS_SQL,
                {"workspace_id": workspace_id, "project_id": project_id_val},
            )
            report_rows = list(cur.fetchall())

        # Build per-project collar lookups for the hole-ID fuzzy match.
        # We do this once per project that has at least one report.
        project_ids_seen = {r["project_id"] for r in report_rows if r["project_id"]}
        project_collar_lookup: dict[str, dict[str, str]] = {}
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for pid in project_ids_seen:
                cur.execute(
                    """
                    SELECT collar_id::text AS collar_id, hole_id_canonical
                    FROM silver.collars
                    WHERE workspace_id = %(workspace_id)s::uuid
                      AND project_id = %(project_id)s::uuid
                      AND hole_id_canonical IS NOT NULL
                    """,
                    {"workspace_id": workspace_id, "project_id": pid},
                )
                lookup: dict[str, str] = {}
                for row in cur.fetchall():
                    canonical = (row["hole_id_canonical"] or "").upper()
                    if canonical:
                        lookup[canonical] = row["collar_id"]
                project_collar_lookup[pid] = lookup

        # --- Pass 1: scan each report -------------------------------------
        for row in report_rows:
            reports_scanned += 1
            report_id = row["report_id"]
            project_id = row["project_id"]
            text = _flatten_sections_text(row["sections_text"])
            # Authors is text[]; treat each entry as a candidate QP name.
            author_names = list(row["authors"] or [])

            contractors = extract_contractors(text)
            geologists = extract_qps(text)
            labs = extract_labs(text)
            holes = extract_hole_ids(text)

            # Merge authors into the QP list — these are already curated
            # per §04i (the report ingest puts them there) so we don't
            # require the credential pattern to fire.
            for a in author_names:
                if not a:
                    continue
                a_clean = a.strip()
                if not a_clean:
                    continue
                if all(_norm(a_clean) != _norm(g) for g in geologists):
                    geologists.append(a_clean)

            contractors_seen_total += len(contractors)
            geologists_seen_total += len(geologists)
            labs_seen_total += len(labs)
            hole_ids_seen_total += len(holes)

            # Stash canonicalized hole-IDs per-project so we can repair
            # legacy NULL silver.collars.hole_id_canonical rows whose
            # hole_id matches the extracted form. canonicalize_hole_id
            # is the same rule the CSV parser uses on INSERT.
            if project_id:
                for hid, _hs, _he in holes:
                    canonical = canonicalize_hole_id(hid)
                    if canonical:
                        ner_canonical_hits.add((project_id, canonical))

            if project_id:
                project_contractors.setdefault(project_id, []).extend(contractors)
                project_geologists.setdefault(project_id, []).extend(geologists)
                project_labs.setdefault(project_id, []).extend(labs)

            # qp_name array per report — uniq-preserving.
            unique_qps: list[str] = []
            seen_qp: set[str] = set()
            for q in geologists:
                k = _norm(q)
                if k and k not in seen_qp:
                    seen_qp.add(k)
                    unique_qps.append(q)
            if unique_qps:
                report_qp_updates.append({
                    "report_id": report_id,
                    "qp_name":   unique_qps,
                })
                for q in unique_qps:
                    qp_report_pairs.append({
                        "name":         q,
                        "report_id":    report_id,
                        "workspace_id": workspace_id,
                    })

            # Per-hole geologist hint — "hole X logged by Y" → Y goes on
            # silver.collars.geologist for X. We re-run the verb pattern
            # against a per-hole window.
            if project_id and project_id in project_collar_lookup:
                lookup = project_collar_lookup[project_id]
                for hid, hs, _he in holes:
                    if hid not in lookup:
                        continue
                    lo = max(0, hs - 80)
                    hi = min(len(text), hs + 200)
                    window = text[lo:hi]
                    persons = extract_qps(window)
                    if persons:
                        # First person mentioned wins for that hole.
                        collar_geologist_hints[(project_id, hid)] = persons[0]

            # Re-anchor input: PR-2 dropped these because sections_text
            # has no collar_id. Pass the text + per-project lookup down.
            if project_id and project_id in project_collar_lookup:
                reanchor_inputs.append((text, project_collar_lookup[project_id]))

        # --- Pass 2: apply UPDATEs (idempotent: WHERE col IS NULL) --------
        campaigns_updated = 0
        collars_updated = 0
        geophys_updated = 0
        assays_updated = 0
        reports_qp_updated = 0
        collars_canonical_repaired = 0

        with conn.cursor() as cur:
            for pid, names in project_contractors.items():
                pick = majority(names)
                if not pick:
                    continue
                cur.execute(
                    UPDATE_CAMPAIGN_CONTRACTOR_SQL,
                    {"workspace_id": workspace_id, "project_id": pid, "contractor": pick},
                )
                campaigns_updated += cur.rowcount
                cur.execute(
                    UPDATE_GEOPHYS_CONTRACTOR_SQL,
                    {"workspace_id": workspace_id, "project_id": pid, "contractor": pick},
                )
                geophys_updated += cur.rowcount

            for pid, names in project_geologists.items():
                pick = majority(names)
                if not pick:
                    continue
                cur.execute(
                    UPDATE_CAMPAIGN_GEOLOGIST_SQL,
                    {"workspace_id": workspace_id, "project_id": pid, "geologist": pick},
                )
                campaigns_updated += cur.rowcount

            for pid, names in project_labs.items():
                pick = majority(names)
                if not pick:
                    continue
                cur.execute(
                    UPDATE_ASSAY_LAB_SQL,
                    {"workspace_id": workspace_id, "project_id": pid, "lab_name": pick},
                )
                assays_updated += cur.rowcount

            for (pid, hole_canonical), geologist in collar_geologist_hints.items():
                cur.execute(
                    UPDATE_COLLAR_GEOLOGIST_SQL,
                    {
                        "workspace_id":      workspace_id,
                        "project_id":        pid,
                        "hole_id_canonical": hole_canonical,
                        "geologist":         geologist,
                    },
                )
                collars_updated += cur.rowcount

            for upd in report_qp_updates:
                cur.execute(UPDATE_REPORT_QP_SQL, upd)
                reports_qp_updated += cur.rowcount

            # Repair legacy NULL hole_id_canonical for collars whose
            # hole_id matches a NER-extracted canonical form. WHERE
            # filters keep this idempotent and workspace-scoped.
            for pid, canonical in ner_canonical_hits:
                cur.execute(
                    UPDATE_COLLAR_CANONICAL_SQL,
                    {
                        "workspace_id":      workspace_id,
                        "project_id":        pid,
                        "hole_id_canonical": canonical,
                    },
                )
                collars_canonical_repaired += cur.rowcount

        # --- Pass 3: re-anchor PR-2 dropped structural rows ---------------
        # Gather all candidates first then dedupe in one shot against the
        # existing structure rows (also dedupes within-batch).
        all_reanchor: list[dict] = []
        for text, lookup in reanchor_inputs:
            all_reanchor.extend(reanchor_candidates(text=text, hole_id_to_collar=lookup))

        existing_keys: set[tuple] = set()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(EXISTING_STRUCTURE_KEYS_SQL, {"workspace_id": workspace_id})
            for r in cur.fetchall():
                existing_keys.add((
                    str(r["collar_id"]) if r["collar_id"] else None,
                    float(r["depth"]) if r["depth"] is not None else None,
                    r["structure_type"],
                    float(r["alpha_angle"]) if r["alpha_angle"] is not None else None,
                    float(r["beta_angle"]) if r["beta_angle"] is not None else None,
                ))

        seen_keys: set[tuple] = set(existing_keys)
        insert_rows: list[dict] = []
        for c in all_reanchor:
            key = (
                c["collar_id"],
                c.get("depth"),
                c["structure_type"],
                c.get("alpha_angle"),
                c.get("beta_angle"),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            insert_rows.append({
                "id":             str(uuid.uuid4()),
                "workspace_id":   workspace_id,
                "collar_id":      c["collar_id"],
                "depth":          c["depth"] if c["depth"] is not None else 0.0,
                "structure_type": c["structure_type"],
                "alpha_angle":    c.get("alpha_angle"),
                "beta_angle":     c.get("beta_angle"),
                "true_dip":       c.get("true_dip"),
                "true_dip_dir":   c.get("true_dip_dir"),
                "notes":          (c.get("notes") or "")[:500],
            })

        structure_inserted = 0
        with conn.cursor() as cur:
            if insert_rows:
                psycopg2.extras.execute_batch(
                    cur, INSERT_STRUCTURE_SQL, insert_rows, page_size=200,
                )
                structure_inserted = len(insert_rows)

        conn.commit()

    # --- Pass 4: Neo4j QP MERGE ------------------------------------------
    # Deduplicate QP+report pairs first; one MERGE per unique (name,
    # report_id, workspace_id) tuple is plenty.
    seen_qp_pair: set[tuple[str, str]] = set()
    qp_unique: list[dict] = []
    for qp in qp_report_pairs:
        k = (_norm(qp["name"]), qp["report_id"])
        if k in seen_qp_pair:
            continue
        seen_qp_pair.add(k)
        qp_unique.append(qp)

    qp_nodes_merged = 0
    if qp_unique:
        driver = neo4j.get_driver()
        try:
            with driver.session() as session:
                session.run(MERGE_QP_CYPHER, {"qps": qp_unique})
                qp_nodes_merged = len({qp["name"] for qp in qp_unique})
        finally:
            driver.close()

    context.log.info(
        "silver_entity_ner_backfill: workspace=%s project=%s reports_scanned=%d "
        "contractors_found=%d geologists_found=%d labs_found=%d hole_ids_found=%d "
        "campaigns_updated=%d collars_updated=%d geophys_updated=%d "
        "assays_updated=%d reports_qp_updated=%d "
        "collars_canonical_repaired=%d structure_reanchored=%d qp_neo4j_merged=%d",
        workspace_id, project_id_val or "(all)", reports_scanned,
        contractors_seen_total, geologists_seen_total, labs_seen_total,
        hole_ids_seen_total, campaigns_updated, collars_updated,
        geophys_updated, assays_updated, reports_qp_updated,
        collars_canonical_repaired, structure_inserted, qp_nodes_merged,
    )

    return MaterializeResult(
        metadata={
            "workspace_id":          MetadataValue.text(workspace_id),
            "project_id":            MetadataValue.text(project_id_val or ""),
            "reports_scanned":       MetadataValue.int(reports_scanned),
            "contractors_found":     MetadataValue.int(contractors_seen_total),
            "geologists_found":      MetadataValue.int(geologists_seen_total),
            "labs_found":            MetadataValue.int(labs_seen_total),
            "hole_ids_found":        MetadataValue.int(hole_ids_seen_total),
            "campaigns_updated":     MetadataValue.int(campaigns_updated),
            "collars_updated":       MetadataValue.int(collars_updated),
            "geophys_updated":       MetadataValue.int(geophys_updated),
            "assays_updated":        MetadataValue.int(assays_updated),
            "reports_qp_updated":    MetadataValue.int(reports_qp_updated),
            "collars_canonical_repaired": MetadataValue.int(collars_canonical_repaired),
            "structure_reanchored":  MetadataValue.int(structure_inserted),
            "qp_nodes_merged":       MetadataValue.int(qp_nodes_merged),
        }
    )


__all__ = [
    "CONTRACTOR_ALLOWLIST",
    "LAB_ALLOWLIST",
    "SilverEntityNerBackfillConfig",
    "silver_entity_ner_backfill",
    "extract_contractors",
    "extract_qps",
    "extract_labs",
    "extract_hole_ids",
    "majority",
    "reanchor_candidates",
]
