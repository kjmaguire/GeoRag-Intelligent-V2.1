"""Gold — cross-corpus linker (Public Geoscience ↔ internal documents).

Phase 3.6. Scaffolding-complete but **ships empty** until SMAD documents
arrive via the standard internal-archive ingestion path (plan §07e).

Responsibilities, per plan §07:

  1. Read every row in `silver.reports` (internal-archive documents including
     future SMAD uploads).
  2. For each document, run three deterministic signal extractors:
       - **SMDI ID match** (`\\bSMDI[-\\s]?\\d{1,5}\\b`) against
         `public_geo.pg_mineral_occurrence.external_id` (renamed
         in V1.2 from `smdi_id`; SK rows still hold SMDI numbers there)
       - **Drillhole ID match** — scan for upstream-assigned drillhole IDs
         that exist in `public_geo.pg_drillhole_collar.drillhole_id`
       - **SMAD filename NTS tile** — parse the NTS 1:250k tile from the
         report title / filename and store it on the Neo4j :Document node
         (plan §07a: "stable external reference anchor"). V1 does NOT
         create REFERENCES edges from NTS tiles alone — pure positional
         matching is a V2 spatial signal.
  3. Write each match as a row in `public_geo.document_entity_links`
     with `signals = ['<signal_name>', …]` and `confidence = 0.95` (plan
     §07a deterministic tier).
  4. Idempotency:
       - If an active link with the same (document, canonical_type, entity)
         already exists and the `signals` set is unchanged, it's a no-op.
       - If `signals` have changed, the old row's `superseded_at` is set
         and a new row is inserted (append-only contract, plan §07b).
  5. Mirror in Neo4j:
       - MERGE (:Document {report_id}) nodes
       - MERGE (:Document)-[:REFERENCES {confidence, signals,
                                         established_at, established_by}]
               ->(:MineralOccurrence | :DrillHole)

Linker is versioned (`established_by = 'linker-v1'`); when V2 signals land
we bump to `linker-v2` and reruns supersede v1 verdicts.

Empty-result behavior: a document producing zero links is NOT a failure —
it's logged informationally (plan §07c). The asset itself never fails on
"no matches"; it only fails on actual infra errors (DB unreachable, etc.).

Signal-type naming convention (§07 addendum):

  Signal names stored in the `signals` JSONB array on each link row
  describe the **extraction method** that established the link, NOT the
  database column the method resolved against. Examples:

    - `smdi_id_match`       — regex matched an SMDI number pattern in
                              document body text, then looked it up in
                              `pg_mineral_occurrence.external_id` (V1.2
                              column rename did not change the signal name
                              because the extraction method is still SMDI-
                              specific and renaming would break stored JSONB
                              in existing `document_entity_links` rows).
    - `drillhole_id_match`  — regex matched a GOS drillhole ID pattern,
                              resolved against `pg_drillhole_collar.drillhole_id`.
    - `nts_filename_match`  — NTS tile parsed from document filename,
                              stored on :Document node for future spatial
                              linking (V2). Not currently used to create
                              REFERENCES edges.

  When adding new signals:
    - Name after the method ("minfile_number_match", "project_name_fuzzy"),
      not the column.
    - Never rename an existing signal — stored JSONB + downstream consumers
      (chat UI signal chips, confidence gating display) depend on stability.
    - Register the signal name in the V2 signal taxonomy when plan §07a
      is extended.

NOTE: Do NOT add `from __future__ import annotations` to this file. Dagster
1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

import psycopg2.extras
from dagster import (
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.assets.gold_public_geoscience import gold_public_geoscience_neo4j
from georag_dagster.resources import Neo4jResource, PostgresResource


LINKER_VERSION = "linker-v1"
DETERMINISTIC_CONFIDENCE = 0.95


# ---------------------------------------------------------------------------
# Regex — deterministic signal extraction
# ---------------------------------------------------------------------------

# Matches "SMDI 0123", "SMDI-0123", "SMDI0123". The numeric group is what
# we normalise on (we strip leading zeros against the SMDI lookup).
_SMDI_RE = re.compile(r"\bSMDI[-\s]?0*([0-9]{1,5})\b", re.IGNORECASE)

# Saskatchewan GOS drillhole IDs have the form GOS_<digits> or GOS-<digits>,
# e.g. GOS_4482, GOS-4482. We also match bare IDs that were already upper-
# cased to match the pg_drillhole_collar.drillhole_id convention.
_GOS_DRILLHOLE_RE = re.compile(r"\bGOS[_\s-]([0-9]{3,6})\b", re.IGNORECASE)

# NTS 1:250,000 tile — 2-3 digit map sheet followed by a single letter
# (74H, 74N, 104P etc.). SMAD filenames embed this before the sequential
# filing number: MAOC_74H-0008_... / AF_74G_0012_...
#
# `\b` doesn't fire between `_` and digits (both are word characters), so
# the original `\b(\d{2,3}[A-P])\b` failed on the most common SMAD filename
# pattern. Use explicit character-class lookarounds: the tile can sit
# between any non-alphanumeric char (incl. underscore) AND must not be
# followed by another alphanumeric (so we don't false-match on "104PA1").
#
# Advisory fixes (V1.2 senior review):
#   - re.IGNORECASE so lowercased document titles ("74h") are still caught.
#     The `_extract_nts_tile` function normalizes to uppercase via `.upper()`.
#   - Lookbehind/lookahead include lowercase letters [a-zA-Z0-9] so
#     "abc74H" in free-text body scans doesn't false-match (lowercase 'c'
#     would have bypassed the old uppercase-only `[A-Z0-9]` guard).
_NTS_TILE_RE = re.compile(
    r"(?<![a-zA-Z0-9])(\d{2,3}[A-Pa-p])(?![a-zA-Z0-9])",
    re.IGNORECASE,
)

# ── Tier 2+3 signal patterns — RESERVED (plan Phases 2–6) ─────────────────
#
# These regexes are declared but NOT YET wired into _scan_document(). They
# sit here so the regex design work (informed by the field inventory in
# docs/field-inventory-sk-tier2-tier3.md) isn't lost while the Silver +
# Bronze assets that would populate their lookup tables are being built.
#
# To activate a pattern:
#   1. Confirm the canonical table exists (pg_mineral_disposition / pg_petroleum_well /
#      pg_geoscience_publication) AND has non-zero rows.
#   2. Write a paired `_load_<signal>_lookup(postgres) -> dict[str, str]`
#      function next to `_load_smdi_lookup` / `_load_drillhole_lookup`.
#   3. Extend the `_scan_document(doc, smdi_lookup, drillhole_lookup, ...)`
#      signature to accept the new lookup + add a scanner block (modelled
#      on the existing SMDI block at lines ~467–486).
#   4. Register the new signal name ("disposition_number_match",
#      "uwi_match", "minfile_number_match", "report_reference_match") in
#      the §07 signal taxonomy and add negative-match test fixtures.
#   5. Bump LINKER_VERSION to "linker-v2" so prior verdicts are superseded
#      cleanly on the next materialization.
#
# Why declared eagerly: the regex authoring work was done while the field
# inventory was fresh; the patterns are tested against sample values in the
# inventory doc. Leaving them here lets the next session focus on the
# lookup + scanner plumbing rather than re-deriving regex shapes.

# Mining disposition numbers. SK publishes several disposition number
# formats across the Mining / Crown Dispositions services:
#   Mineral dispositions:  "CBS-123456", "MC-1234", "MC 1234", "CBS 123456"
#   Potash dispositions:   "P-5432", "KP-1234"
#   Coal dispositions:     "CL-123", "CBL-123"
#   Alkali dispositions:   "CP-123", "ALK-12"
#   Quarry dispositions:   "Q-12345"
#   Oil and Gas (Crown):   numeric ID, e.g. "12345" — too generic to match
#                          without context, so gated by petroleum context word.
# The conservative strategy: match the alpha prefix + optional separator +
# digits; gate on the capture group being 3+ digits so short license numbers
# don't false-match common non-disposition strings.
_MINING_DISPOSITION_RE = re.compile(  # noqa: F841 — reserved, see header above
    r"\b(CBS|MC|P|KP|CL|CBL|CP|ALK|Q|QR)[-\s]?(\d{3,6})\b",
    re.IGNORECASE,
)

# BC MINFILE numbers have the form NN[A-Z] NNN, e.g. "093A 123", "082F 456".
# Tightly anchored to avoid over-matching general NTS tile patterns.
_MINFILE_RE = re.compile(  # noqa: F841 — reserved, see header above
    r"\b(\d{2,3}[A-P])[-\s]?(\d{3,4})\b",
)

# UWI (Dominion Land Survey) for petroleum wells — "101/01-23-045-06W3/0"
# or variants without slashes. The canonical SK form uses 'W' followed by
# the meridian digit. Tightly anchored because it's a distinctive shape.
_UWI_RE = re.compile(  # noqa: F841 — reserved, see header above
    r"\b(\d{3})[/\s-](\d{2})-(\d{2})-(\d{3})-(\d{2})W(\d)(?:[/\s-](\d))?\b",
)

# SGS report + map references. Fields in pg_geoscience_publication are
# REPORT_NUMBER ("Rep 123", "R-123", "SGS-R-123") and MAP_NUMBER
# ("Map 100-10", "Map 99-1"). Match both with a loose join.
_REPORT_NUMBER_RE = re.compile(  # noqa: F841 — reserved, see header above
    r"\b(?:Report|Rep|R)[-\s]?(\d{1,4}(?:-\d{1,4})?)\b",
    re.IGNORECASE,
)
_MAP_NUMBER_RE = re.compile(  # noqa: F841 — reserved, see header above
    r"\bMap[-\s]?(\d{1,4}-\d{1,4})\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class DocumentRow:
    report_id: str
    title: str
    filename: str | None
    nts_tile: str | None
    body_text: str                  # concatenated sections_text for scanning


@dataclass
class ProposedLink:
    document_id: str                # silver.reports.report_id
    document_filename: str | None
    canonical_type: str             # "mineral_occurrence" | "drillhole_collar"
    entity_id: str                  # pg_*.id (UUID)
    confidence: float
    signals: list[str]              # ordered, dedup'd
    extracted_context: str | None   # short human-readable snippet ("matched SMDI 0123")


@dataclass
class LinkerStats:
    documents_scanned: int = 0
    documents_with_matches: int = 0
    proposed_links: int = 0
    new_links_inserted: int = 0
    links_superseded: int = 0
    links_noop_unchanged: int = 0
    nts_tiles_seen: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Postgres access
# ---------------------------------------------------------------------------

def _load_documents(postgres: PostgresResource) -> list[DocumentRow]:
    """Read silver.reports into scannable DocumentRow records.

    sections_text is stored as a JSONB object whose values are strings. We
    concatenate all values into a single body_text blob so the regex scanners
    don't care about document structure.

    SMAD documents haven't arrived yet in V1; when silver.reports is empty
    we return [] and the asset becomes a no-op (plan §07e).
    """
    out: list[DocumentRow] = []
    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    report_id::text   AS report_id,
                    title,
                    sections_text
                  FROM silver.reports
                 ORDER BY filing_date DESC NULLS LAST, title
                """
            )
            rows = cur.fetchall()

    for r in rows:
        title = str(r.get("title") or "")
        sections = r.get("sections_text") or {}
        if isinstance(sections, str):
            # Some drivers return JSONB as a string
            try:
                sections = json.loads(sections)
            except (TypeError, ValueError):
                sections = {}

        body_parts: list[str] = []
        if isinstance(sections, dict):
            for value in sections.values():
                if value is None:
                    continue
                if isinstance(value, str):
                    body_parts.append(value)
                else:
                    body_parts.append(json.dumps(value))

        body_text = "\n".join(body_parts)
        nts = _extract_nts_tile(title) or _extract_nts_tile(body_text[:4096])

        out.append(
            DocumentRow(
                report_id=str(r["report_id"]),
                title=title,
                filename=_guess_filename(title),
                nts_tile=nts,
                body_text=body_text,
            )
        )
    return out


def _load_smdi_lookup(postgres: PostgresResource) -> dict[str, str]:
    """Map normalized SMDI number → pg_mineral_occurrence.id.

    The linker's SMDI regex (_SMDI_RE) is SK-specific (matches strings
    like "SMDI 0123" in document body text). The underlying canonical
    column was renamed in V1.2 from `smdi_id` → `external_id` because
    BC MINFILE + future jurisdictions populate the same slot with their
    own jurisdiction-native identifiers. The linker still scopes its
    lookup to rows whose external_id parses as SMDI-shaped (the DB
    string is stored as digits) so we can match the SMDI regex hits
    cleanly. Future BC MINFILE_NUMBER linker support adds a parallel
    `_load_minfile_lookup` + `_MINFILE_RE` rather than reshaping this.
    """
    lookup: dict[str, str] = {}
    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id::text AS id, external_id
                  FROM public_geo.pg_mineral_occurrence
                 WHERE external_id IS NOT NULL
                   AND external_id <> ''
                   AND jurisdiction_code = 'CA-SK'
                """
            )
            for row in cur.fetchall():
                key = _normalize_smdi(row["external_id"])
                if key:
                    lookup[key] = row["id"]
    return lookup


def _load_drillhole_lookup(postgres: PostgresResource) -> dict[str, str]:
    """Map normalized drillhole_id → pg_drillhole_collar.id.

    Upstream IDs are Saskatchewan's GOS_UNIQUE_DRILLHOLE_ID (e.g. "GOS_4482").
    We store a case-insensitive key with separator stripped.
    """
    lookup: dict[str, str] = {}
    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id::text AS id, drillhole_id
                  FROM public_geo.pg_drillhole_collar
                 WHERE drillhole_id IS NOT NULL AND drillhole_id <> ''
                """
            )
            for row in cur.fetchall():
                key = _normalize_drillhole_id(row["drillhole_id"])
                if key:
                    lookup[key] = row["id"]
    return lookup


def _fetch_existing_active_links(
    postgres: PostgresResource,
    document_ids: list[str],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Return the set of currently active (superseded_at IS NULL) links for
    the given documents, keyed on (document_id, canonical_type, entity_id)
    so the upsert can compare proposed vs. existing signal sets.
    """
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    if not document_ids:
        return out
    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, document_id::text AS document_id,
                       canonical_type,
                       entity_id::text     AS entity_id,
                       confidence, signals,
                       established_by
                  FROM public_geo.document_entity_links
                 WHERE superseded_at IS NULL
                   AND document_id = ANY(%s::uuid[])
                """,
                (document_ids,),
            )
            for r in cur.fetchall():
                key = (r["document_id"], r["canonical_type"], r["entity_id"])
                out[key] = dict(r)
    return out


def _load_all_active_links(postgres: PostgresResource) -> list[ProposedLink]:
    """Return every currently-active row in document_entity_links as a
    ProposedLink so the Neo4j mirror can MERGE from PG on every run.

    Phase-3.6 originally mirrored only the newly-inserted batch. That made
    the Neo4j projection drift whenever a mirror attempt failed: the PG
    audit trail committed, the graph never caught up, and the next run
    treated the link as `unchanged` — skipping the mirror entirely (plan
    §07c "no silent partial failures" violation).
    Fix: PG is the source of truth, Neo4j is an idempotent projection.
    Every run MERGE-s the full active set; MERGE semantics make repeats
    cheap and a failed mirror self-heals on the next materialization.
    """
    out: list[ProposedLink] = []
    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT document_id::text AS document_id,
                       document_filename,
                       canonical_type,
                       entity_id::text   AS entity_id,
                       confidence,
                       signals,
                       extracted_context,
                       established_by
                  FROM public_geo.document_entity_links
                 WHERE superseded_at IS NULL
                """
            )
            for r in cur.fetchall():
                out.append(
                    ProposedLink(
                        document_id=r["document_id"],
                        document_filename=r.get("document_filename"),
                        canonical_type=r["canonical_type"],
                        entity_id=r["entity_id"],
                        confidence=float(r["confidence"] or 0.0),
                        signals=list(r.get("signals") or []),
                        extracted_context=r.get("extracted_context"),
                    )
                )
    return out


def _apply_links(
    postgres: PostgresResource,
    proposed: list[ProposedLink],
    stats: LinkerStats,
    context: AssetExecutionContext,
) -> list[ProposedLink]:
    """Apply the append-only upsert for a batch of proposed links.

    Returns the list of links that were actually inserted (new rows),
    excluding no-ops. Used downstream for the Neo4j REFERENCES mirror.
    """
    if not proposed:
        return []

    document_ids = sorted({p.document_id for p in proposed})
    existing = _fetch_existing_active_links(postgres, document_ids)
    newly_inserted: list[ProposedLink] = []

    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            for link in proposed:
                key = (link.document_id, link.canonical_type, link.entity_id)
                old = existing.get(key)

                signals_sorted = sorted(set(link.signals))

                if old is not None:
                    old_signals = sorted(old.get("signals") or [])
                    old_confidence = float(old.get("confidence") or 0.0)
                    unchanged = (
                        old_signals == signals_sorted
                        and abs(old_confidence - link.confidence) < 0.0005
                    )
                    if unchanged:
                        stats.links_noop_unchanged += 1
                        continue
                    # Supersede old, insert new.
                    cur.execute(
                        """
                        UPDATE public_geo.document_entity_links
                           SET superseded_at = NOW()
                         WHERE id = %s
                        """,
                        (old["id"],),
                    )
                    stats.links_superseded += 1
                    supersedes_id = old["id"]
                else:
                    supersedes_id = None

                cur.execute(
                    """
                    INSERT INTO public_geo.document_entity_links (
                        document_id, document_filename,
                        canonical_type, entity_id,
                        confidence, signals, extracted_context,
                        established_at, established_by, supersedes_id
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s::jsonb, %s,
                        NOW(), %s, %s
                    )
                    """,
                    (
                        link.document_id,
                        link.document_filename,
                        link.canonical_type,
                        link.entity_id,
                        link.confidence,
                        json.dumps(signals_sorted),
                        link.extracted_context,
                        LINKER_VERSION,
                        supersedes_id,
                    ),
                )
                stats.new_links_inserted += 1
                newly_inserted.append(link)

        conn.commit()

    if stats.new_links_inserted or stats.links_superseded:
        context.log.info(
            "cross_corpus_linker: postgres write complete — new=%d superseded=%d noop=%d",
            stats.new_links_inserted,
            stats.links_superseded,
            stats.links_noop_unchanged,
        )
    return newly_inserted


# ---------------------------------------------------------------------------
# Signal extraction
# ---------------------------------------------------------------------------

def _scan_document(
    doc: DocumentRow,
    smdi_lookup: dict[str, str],
    drillhole_lookup: dict[str, str],
) -> list[ProposedLink]:
    """Run all deterministic extractors against one document and return
    per-match proposed link records."""
    links: list[ProposedLink] = []

    # ── SMDI ID matches ───────────────────────────────────────────────
    seen_smdi: set[str] = set()
    for match in _SMDI_RE.finditer(doc.body_text):
        smdi_key = match.group(1).lstrip("0") or "0"
        if smdi_key in seen_smdi:
            continue
        seen_smdi.add(smdi_key)
        entity_id = smdi_lookup.get(smdi_key)
        if not entity_id:
            continue
        context = _surrounding_context(doc.body_text, match.start(), match.end())
        links.append(
            ProposedLink(
                document_id=doc.report_id,
                document_filename=doc.filename,
                canonical_type="mineral_occurrence",
                entity_id=entity_id,
                confidence=DETERMINISTIC_CONFIDENCE,
                signals=["smdi_id_match"],
                extracted_context=context,
            )
        )

    # ── Drillhole ID matches ──────────────────────────────────────────
    seen_dh: set[str] = set()
    for match in _GOS_DRILLHOLE_RE.finditer(doc.body_text):
        key = _normalize_drillhole_id(f"GOS_{match.group(1)}")
        if key in seen_dh:
            continue
        seen_dh.add(key)
        entity_id = drillhole_lookup.get(key)
        if not entity_id:
            continue
        context = _surrounding_context(doc.body_text, match.start(), match.end())
        links.append(
            ProposedLink(
                document_id=doc.report_id,
                document_filename=doc.filename,
                canonical_type="drillhole_collar",
                entity_id=entity_id,
                confidence=DETERMINISTIC_CONFIDENCE,
                signals=["drillhole_id_match"],
                extracted_context=context,
            )
        )

    return links


def _extract_nts_tile(text: str) -> str | None:
    if not text:
        return None
    m = _NTS_TILE_RE.search(text)
    return m.group(1).upper() if m else None


def _normalize_smdi(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    # Drop non-digits, then strip leading zeros.
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return None
    stripped = digits.lstrip("0") or "0"
    return stripped


def _normalize_drillhole_id(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    return re.sub(r"[\s_\-]+", "_", s)


def _guess_filename(title: str) -> str | None:
    if not title:
        return None
    # SMAD filenames typically look like `MAOC_74H-0008_Report.pdf`; titles
    # may or may not include the .pdf extension. We peel off any path prefix
    # and return the first token that looks filename-ish.
    m = re.search(r"([A-Za-z0-9_\-]+\.(?:pdf|zip))", title, re.IGNORECASE)
    if m:
        return m.group(1)
    # Fallback: if the title starts with a SMAD-style prefix like "MAOC_74H"
    # treat the full title as the filename.
    if re.match(r"^[A-Z]{2,5}_\d{2,3}[A-P][-_]", title):
        return title
    return None


def _surrounding_context(text: str, start: int, end: int, radius: int = 60) -> str:
    """Return a short ±radius-char window around the matched span, trimmed
    to word boundaries. Stored on the link row for auditability.
    """
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    window = text[lo:hi].replace("\n", " ").strip()
    return window


# ---------------------------------------------------------------------------
# Neo4j mirror — :Document + :REFERENCES
# ---------------------------------------------------------------------------

_CANONICAL_TYPE_TO_LABEL: dict[str, str] = {
    "mine":                    "Mine",
    "mineral_occurrence":      "MineralOccurrence",
    "drillhole_collar":        "Drillhole",
    "resource_potential_zone": "ResourcePotentialZone",
}

MERGE_DOCUMENT_CYPHER = """
UNWIND $rows AS r
MERGE (d:Document {report_id: r.report_id})
  ON CREATE SET
    d.title      = r.title,
    d.filename   = r.filename,
    d.nts_tile   = r.nts_tile,
    d.created_at = datetime()
  ON MATCH SET
    d.title      = r.title,
    d.filename   = r.filename,
    d.nts_tile   = r.nts_tile,
    d.last_updated = datetime()
"""

# Note the dynamic label on the target node — we build one Cypher string per
# canonical_type rather than using apoc.merge.node (Community Edition doesn't
# ship APOC by default, per CLAUDE.md rule 9).
def _merge_references_cypher(label: str) -> str:
    return f"""
UNWIND $rows AS r
MATCH (d:Document {{report_id: r.document_id}})
MATCH (e:{label} {{pg_id: r.entity_id}})
MERGE (d)-[ref:REFERENCES]->(e)
  ON CREATE SET
    ref.confidence     = r.confidence,
    ref.signals        = r.signals,
    ref.established_at = datetime(),
    ref.established_by = r.established_by
  ON MATCH SET
    ref.confidence     = r.confidence,
    ref.signals        = r.signals,
    ref.established_by = r.established_by,
    ref.last_updated   = datetime()
"""


def _write_neo4j(
    neo4j: Neo4jResource,
    documents: list[DocumentRow],
    links: list[ProposedLink],
    context: AssetExecutionContext,
) -> dict[str, int]:
    """Mirror the Postgres link table into Neo4j (idempotent projection).

    `links` is the FULL active set for this run — every active
    document_entity_links row, not just newly-inserted rows. Every MERGE
    is idempotent so re-running against unchanged links is cheap; this
    lets the graph self-heal after a previous mirror failure.

    Writes:
      - :Document nodes (one MERGE per report regardless of link count)
      - (:Document)-[:REFERENCES]->(:Mine|:MineralOccurrence|:DrillHole|
                                    :ResourcePotentialZone) edges

    Returns per-label edge counts for the MaterializeResult metadata.
    """
    counts: dict[str, int] = {}
    if not documents:
        return counts

    driver = neo4j.get_driver()
    try:
        with driver.session(database="neo4j") as session:
            # ── Constraints ──────────────────────────────────────────
            session.run(
                "CREATE CONSTRAINT document_report_id IF NOT EXISTS "
                "FOR (d:Document) REQUIRE d.report_id IS UNIQUE"
            )

            # ── :Document MERGE ───────────────────────────────────────
            doc_rows = [
                {
                    "report_id": d.report_id,
                    "title": d.title,
                    "filename": d.filename,
                    "nts_tile": d.nts_tile,
                }
                for d in documents
            ]
            with session.begin_transaction() as tx:
                tx.run(MERGE_DOCUMENT_CYPHER, rows=doc_rows)
                tx.commit()

            # ── :REFERENCES edges (one MERGE per canonical_type) ──────
            if not links:
                context.log.info(
                    "cross_corpus_linker: no active links to mirror into Neo4j "
                    "(document_entity_links is empty or all superseded)",
                )
                return counts

            by_type: dict[str, list[dict[str, Any]]] = {}
            for link in links:
                by_type.setdefault(link.canonical_type, []).append(
                    {
                        "document_id": link.document_id,
                        "entity_id": link.entity_id,
                        "confidence": link.confidence,
                        "signals": sorted(set(link.signals)),
                        "established_by": LINKER_VERSION,
                    }
                )

            for canonical_type, rows in by_type.items():
                label = _CANONICAL_TYPE_TO_LABEL.get(canonical_type)
                if label is None:
                    context.log.warning(
                        "cross_corpus_linker: unknown canonical_type=%s, skipping",
                        canonical_type,
                    )
                    continue
                cypher = _merge_references_cypher(label)
                with session.begin_transaction() as tx:
                    tx.run(cypher, rows=rows)
                    tx.commit()
                counts[canonical_type] = len(rows)

    finally:
        driver.close()

    return counts


# ---------------------------------------------------------------------------
# Asset config
# ---------------------------------------------------------------------------

class CrossCorpusLinkerConfig(Config):
    """Runtime knobs for the linker."""

    # When True, skip the Neo4j mirror and only write Postgres link rows.
    # Useful for dev runs against a Neo4j-less stack.
    skip_neo4j: bool = False

    # When True, the linker only *prints* proposed links without writing.
    # Handy for dry-running new signal extractors on historical documents.
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="gold",
    # Depends on the Gold Neo4j asset so entity nodes exist before we try to
    # MATCH them for the REFERENCES edge. Also depends on silver.reports
    # implicitly via the silver_reports asset (not imported here to avoid
    # circular — the dep is soft: zero reports → zero links, no hard failure).
    deps=[gold_public_geoscience_neo4j],
    description=(
        "Cross-corpus linker (plan §07). Scans silver.reports for "
        "deterministic references to Public Geoscience entities — SMDI IDs, "
        "drillhole IDs, and SMAD-filename NTS tiles — and writes "
        "(:Document)-[:REFERENCES]->(:Mine|:MineralOccurrence|:DrillHole|"
        ":ResourcePotentialZone) edges plus mirror rows in "
        "public_geo.document_entity_links. V1 deterministic-only; "
        "structural/spatial/textual signals deferred to V2 per §07f. Ships "
        "empty and produces zero links until SMAD documents arrive."
    ),
)
def gold_cross_corpus_linker(
    context: AssetExecutionContext,
    config: CrossCorpusLinkerConfig,
    postgres: PostgresResource,
    neo4j: Neo4jResource,
) -> MaterializeResult:
    stats = LinkerStats()

    # ── Load documents + lookup tables in parallel reads ───────────────
    documents = _load_documents(postgres)
    stats.documents_scanned = len(documents)

    if not documents:
        context.log.info(
            "cross_corpus_linker: silver.reports is empty — ships-empty state, "
            "returning without writing. Asset is fully scaffolded and will "
            "activate automatically once SMAD documents ingest.",
        )
        return MaterializeResult(
            metadata={
                "documents_scanned":     MetadataValue.int(0),
                "documents_with_matches": MetadataValue.int(0),
                "proposed_links":        MetadataValue.int(0),
                "new_links_inserted":    MetadataValue.int(0),
                "links_superseded":      MetadataValue.int(0),
                "links_noop_unchanged":  MetadataValue.int(0),
                "linker_version":        MetadataValue.text(LINKER_VERSION),
                "empty_scaffolding":     MetadataValue.bool(True),
            }
        )

    smdi_lookup = _load_smdi_lookup(postgres)
    drillhole_lookup = _load_drillhole_lookup(postgres)
    context.log.info(
        "cross_corpus_linker: loaded %d docs, %d SMDI entities, %d drillhole entities",
        len(documents), len(smdi_lookup), len(drillhole_lookup),
    )

    # ── Scan each document; record NTS tiles for metadata. ─────────────
    proposed: list[ProposedLink] = []
    for doc in documents:
        if doc.nts_tile:
            stats.nts_tiles_seen[doc.nts_tile] = stats.nts_tiles_seen.get(doc.nts_tile, 0) + 1
        per_doc = _scan_document(doc, smdi_lookup, drillhole_lookup)
        if per_doc:
            stats.documents_with_matches += 1
            proposed.extend(per_doc)

    stats.proposed_links = len(proposed)
    context.log.info(
        "cross_corpus_linker: scanned %d docs, %d produced matches, %d links proposed",
        stats.documents_scanned, stats.documents_with_matches, stats.proposed_links,
    )

    if config.dry_run:
        context.log.info("cross_corpus_linker: dry_run=True — not writing")
        return MaterializeResult(
            metadata=_stats_to_metadata(stats, dry_run=True),
        )

    # ── Append-only write to Postgres. ─────────────────────────────────
    _apply_links(postgres, proposed, stats, context)

    # ── Neo4j mirror (idempotent, full-set projection). ────────────────
    # We MERGE every currently-active PG link on every run — not just the
    # batch newly inserted this run. Rationale:
    #   1. MERGE is idempotent, so re-mirroring unchanged links is a no-op
    #      on Neo4j (property SET is cheap).
    #   2. If a previous run's mirror failed mid-way, the PG audit trail
    #      committed but the graph drifted. Treating Neo4j as a derived
    #      projection that rebuilds from PG on every run heals that drift
    #      automatically on the next materialization.
    # Exception on failure still propagates per plan §07c "no silent
    # partial failures" — Dagster marks the run red and the next run
    # re-projects from PG.
    neo4j_counts: dict[str, int] = {}
    if not config.skip_neo4j:
        try:
            active_links = _load_all_active_links(postgres)
            context.log.info(
                "cross_corpus_linker: mirroring %d active PG links into Neo4j "
                "(newly inserted this run: %d, superseded: %d)",
                len(active_links),
                stats.new_links_inserted,
                stats.links_superseded,
            )
            neo4j_counts = _write_neo4j(neo4j, documents, active_links, context)
        except Exception:
            context.log.exception(
                "cross_corpus_linker: Neo4j mirror failed — PG link rows are "
                "committed; next run will reproject them from the active set."
            )
            raise

    return MaterializeResult(
        metadata={
            **_stats_to_metadata(stats, dry_run=False),
            "neo4j_references_by_type": MetadataValue.json(neo4j_counts),
        }
    )


def _stats_to_metadata(stats: LinkerStats, *, dry_run: bool) -> dict[str, Any]:
    return {
        "documents_scanned":      MetadataValue.int(stats.documents_scanned),
        "documents_with_matches": MetadataValue.int(stats.documents_with_matches),
        "proposed_links":         MetadataValue.int(stats.proposed_links),
        "new_links_inserted":     MetadataValue.int(stats.new_links_inserted),
        "links_superseded":       MetadataValue.int(stats.links_superseded),
        "links_noop_unchanged":   MetadataValue.int(stats.links_noop_unchanged),
        "linker_version":         MetadataValue.text(LINKER_VERSION),
        "nts_tiles_seen":         MetadataValue.json(stats.nts_tiles_seen),
        "dry_run":                MetadataValue.bool(dry_run),
        "empty_scaffolding":      MetadataValue.bool(stats.proposed_links == 0),
    }
