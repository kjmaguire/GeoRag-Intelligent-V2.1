"""PDF ingester for Wyoming Cameco / WSGS uranium drillhole archive.

Doc-phase 179 — Phase B Tier 1.

Reads native PDF text via `pdfminer.six`, chunks by paragraph + page
boundaries, lands rows in `silver.documents` (via `reports` if applicable)
+ `silver.document_passages`.

For Phase B Tier 1, we ingest only native-text PDFs (no OCR fallback).
Scanned PDFs that produce empty extracted text are flagged for the
§04p OCR pipeline as a follow-on tick.

Chunking strategy:
  - Group consecutive paragraphs until ~800 characters
  - Hard break on page boundaries
  - Min chunk: 100 characters (avoid TOC entries / page numbers)
  - Max chunk: 1200 characters
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import asyncpg

log = logging.getLogger("georag.ingest.pdf")


_MIN_CHUNK = 100
_TARGET_CHUNK = 800
_MAX_CHUNK = 1200

# Plan §1b — parent-child chunking. Default group size when the flag is
# on but PARENT_CHUNKING_GROUP_SIZE isn't explicitly set. Mirrored from
# app.config.settings default so unit tests can drive _group_into_parents
# directly without a settings stub.
_DEFAULT_PARENT_GROUP_SIZE = 3


@dataclass
class PDFIngestResult:
    file_path: str
    document_id: str | None
    passages_inserted: int
    page_count: int
    skipped: bool = False
    skipped_reason: str | None = None
    error: str | None = None


def _extract_text_pages(pdf_path: str) -> list[str]:
    """Extract text per page using pdfminer.six. Returns list[page_text].

    Empty-text pages (scanned, no embedded text) come back as empty
    strings — the caller decides how to handle.
    """
    from pdfminer.high_level import extract_text

    # pdfminer doesn't natively expose per-page; use extract_pages instead
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer

    pages = []
    for page_layout in extract_pages(pdf_path):
        page_text_parts = []
        for element in page_layout:
            if isinstance(element, LTTextContainer):
                page_text_parts.append(element.get_text())
        pages.append("".join(page_text_parts).strip())
    return pages


_PARA_SPLIT_RE = re.compile(r"\n\s*\n+")
_WHITESPACE_NORM_RE = re.compile(r"[ \t]+")


def _chunk_pages(
    pages: list[str],
    *,
    parent_chunking: bool | None = None,
    parents_per_group: int | None = None,
) -> list[dict]:
    """Chunk extracted page text into passage records.

    Per Plan §1b, dispatches between two strategies:

      1. **Flat narrative** (legacy, default). Each row is a standalone
         passage with ``chunk_kind='narrative'`` and ``parent_chunk_id=None``.
         Returns list of dicts with: text, ordinal, page_first, page_last,
         text_hash, chunk_kind, parent_chunk_id.

      2. **Parent-child** (new, behind ``settings.PARENT_CHUNKING_ENABLED``).
         Groups every ``N`` contiguous child chunks under a parent passage.
         Parent rows carry ``chunk_kind='section'`` + ``parent_chunk_id=None``
         and ``passage_id_override`` (a pre-generated UUID); children carry
         ``chunk_kind='paragraph'`` + ``parent_chunk_id`` = that parent's
         UUID. See ``docs/architecture/parent_child_chunker_spec.md``.

    Args:
        pages: page texts from ``_extract_text_pages``
        parent_chunking: explicit override (None → read from settings).
            Tests pass an explicit bool to avoid settings stubs.
        parents_per_group: explicit group size (None → read from settings).

    Backward-compatibility: legacy callers calling ``_chunk_pages(pages)``
    with no kwargs get the flag-driven dispatch — defaults to flat when
    the flag is off, so production behaviour is unchanged until the
    operator flips ``PARENT_CHUNKING_ENABLED``.
    """
    if parent_chunking is None:
        try:
            from app.config import settings  # noqa: PLC0415
            parent_chunking = bool(getattr(settings, "PARENT_CHUNKING_ENABLED", False))
            if parents_per_group is None:
                parents_per_group = int(
                    getattr(settings, "PARENT_CHUNKING_GROUP_SIZE", _DEFAULT_PARENT_GROUP_SIZE)
                )
        except Exception:  # pragma: no cover — defensive (settings unavailable in raw tests)
            parent_chunking = False

    if parents_per_group is None:
        parents_per_group = _DEFAULT_PARENT_GROUP_SIZE

    children = _chunk_pages_flat(pages)
    if not parent_chunking:
        return children

    return _group_into_parents(children, parents_per_group=parents_per_group)


def _chunk_pages_flat(pages: list[str]) -> list[dict]:
    """Legacy flat-narrative chunker (renamed from the original ``_chunk_pages``).

    Each output dict carries the new ``chunk_kind='narrative'`` +
    ``parent_chunk_id=None`` fields so the ``_insert_passages`` SQL has
    uniform shape whether parent-chunking is on or off.
    """
    chunks: list[dict] = []
    ordinal = 0
    for page_idx, page_text in enumerate(pages, start=1):
        if not page_text or len(page_text) < _MIN_CHUNK:
            continue
        # Split into paragraphs
        paragraphs = [_WHITESPACE_NORM_RE.sub(" ", p).strip()
                      for p in _PARA_SPLIT_RE.split(page_text)]
        paragraphs = [p for p in paragraphs if p]

        buf = ""
        for para in paragraphs:
            if len(buf) + len(para) + 1 > _MAX_CHUNK and buf:
                # Flush
                if len(buf) >= _MIN_CHUNK:
                    chunks.append({
                        "ordinal": ordinal,
                        "text": buf.strip(),
                        "page_first": page_idx,
                        "page_last": page_idx,
                        "text_hash": hashlib.sha256(buf.strip().encode()).hexdigest(),
                        "chunk_kind": "narrative",
                        "parent_chunk_id": None,
                    })
                    ordinal += 1
                buf = para
            elif len(buf) >= _TARGET_CHUNK:
                # Soft flush
                if len(buf) >= _MIN_CHUNK:
                    chunks.append({
                        "ordinal": ordinal,
                        "text": buf.strip(),
                        "page_first": page_idx,
                        "page_last": page_idx,
                        "text_hash": hashlib.sha256(buf.strip().encode()).hexdigest(),
                        "chunk_kind": "narrative",
                        "parent_chunk_id": None,
                    })
                    ordinal += 1
                buf = para
            else:
                buf = (buf + "\n" + para).strip() if buf else para

        # Flush remainder at page boundary
        if buf and len(buf) >= _MIN_CHUNK:
            chunks.append({
                "ordinal": ordinal,
                "text": buf.strip(),
                "page_first": page_idx,
                "page_last": page_idx,
                "text_hash": hashlib.sha256(buf.strip().encode()).hexdigest(),
                "chunk_kind": "narrative",
                "parent_chunk_id": None,
            })
            ordinal += 1
    return chunks


def _group_into_parents(
    children: list[dict],
    *,
    parents_per_group: int = _DEFAULT_PARENT_GROUP_SIZE,
) -> list[dict]:
    """Emit interleaved parent + child rows per Plan §1b §7.1.

    For each contiguous group of ``parents_per_group`` children:
      • Emit a parent passage with pre-generated UUID, text = ``\\n\\n`` join
        of the children's text, page span = first child's page_first to
        last child's page_last, ``chunk_kind='section'``.
      • Emit each child of the group with ``chunk_kind='paragraph'`` and
        ``parent_chunk_id`` = that parent UUID.

    Edge cases (per spec §2 + §8):
      • Tail group of size 1 → emit as ``chunk_kind='narrative'`` with
        no parent (avoids 1-child parents that just duplicate the child).
      • Empty input → empty output.
      • Single child total → single narrative row, no parent.

    Ordinals are re-numbered in document order (parent before its children).
    """
    if not children:
        return []

    if parents_per_group < 2:
        raise ValueError(
            f"parents_per_group must be ≥ 2 to form a parent "
            f"(got {parents_per_group}); use parent_chunking=False for flat output"
        )

    out: list[dict] = []
    ordinal = 0
    for group_start in range(0, len(children), parents_per_group):
        group = children[group_start : group_start + parents_per_group]

        # Tail-single: emit flat (no parent), keeps legacy behaviour for
        # the lone trailing child rather than creating a useless 1-child
        # parent that just duplicates the child.
        if len(group) == 1:
            c = dict(group[0])
            c["ordinal"] = ordinal
            c["chunk_kind"] = "narrative"
            c["parent_chunk_id"] = None
            # Drop any pre-existing passage_id_override from upstream;
            # flat narrative rows let SQL generate the UUID.
            c.pop("passage_id_override", None)
            out.append(c)
            ordinal += 1
            continue

        # Parent: concat of children's text. Pre-generate UUID here so
        # children below can carry the FK without a two-pass insert.
        parent_id = str(uuid.uuid4())
        parent_text = "\n\n".join(c["text"] for c in group)
        out.append({
            "passage_id_override": parent_id,
            "ordinal": ordinal,
            "text": parent_text,
            "text_hash": hashlib.sha256(parent_text.encode()).hexdigest(),
            "page_first": group[0]["page_first"],
            "page_last": group[-1]["page_last"],
            "chunk_kind": "section",
            "parent_chunk_id": None,
        })
        ordinal += 1

        for child in group:
            c = dict(child)
            c["ordinal"] = ordinal
            c["chunk_kind"] = "paragraph"
            c["parent_chunk_id"] = parent_id
            # Drop any pre-existing override from upstream; children let
            # SQL generate their own UUIDs (only parents pre-generate).
            c.pop("passage_id_override", None)
            out.append(c)
            ordinal += 1

    return out


async def _get_or_create_document(
    conn: asyncpg.Connection,
    *,
    file_path: str,
    title: str,
    project_id: str | None,
    workspace_id: str,
    source_sha256: str,
    company: str | None = None,
    region: str | None = None,
    is_scanned: bool = False,
) -> str:
    """Idempotently fetch or create a `silver.reports` row.

    Returns the report_id (UUID as string) — used as document_id in
    silver.document_passages.
    """
    # Reuse if same file content already ingested (dedupe by sha)
    row = await conn.fetchrow(
        """
        SELECT report_id::text AS report_id
          FROM silver.reports
         WHERE source_file_sha256 = $1
         LIMIT 1
        """,
        source_sha256,
    )
    if row:
        return row["report_id"]
    row = await conn.fetchrow(
        """
        INSERT INTO silver.reports
            (report_id, project_id, workspace_id, title, company, region,
             commodity, source_file_sha256, is_scanned, parser_used,
             created_at, updated_at)
        VALUES (gen_random_uuid(), $1::uuid, $2::uuid, $3, $4, $5,
                'uranium', $6, $7, 'pdfminer.six',
                NOW(), NOW())
        RETURNING report_id::text AS report_id
        """,
        project_id, workspace_id, title[:500], company, region,
        source_sha256, is_scanned,
    )
    return row["report_id"]


async def _insert_passages(
    conn: asyncpg.Connection,
    *,
    document_id: str,
    workspace_id: str,
    chunks: list[dict],
    revision_number: int = 1,
) -> int:
    """Bulk-insert passages with ON CONFLICT DO NOTHING for dedup.

    Per Plan §1b, each chunk dict may carry:
      • ``passage_id_override`` — pre-generated UUID (parent rows only;
        children let SQL generate via ``gen_random_uuid()``). Children's
        ``parent_chunk_id`` references the parent's override.
      • ``chunk_kind`` — ``'narrative'`` (legacy / flat), ``'section'``
        (parent), or ``'paragraph'`` (child). Defaults to ``'narrative'``
        when missing, preserving pre-§1b call-site behaviour.
      • ``parent_chunk_id`` — UUID of the parent passage for children,
        ``None`` otherwise.

    Insertion order matters: parents BEFORE children, so the child's
    FK reference points at an existing row. ``_group_into_parents``
    interleaves them correctly; this function preserves caller order.
    """
    inserted = 0
    for c in chunks:
        try:
            r = await conn.fetchrow(
                """
                INSERT INTO silver.document_passages
                    (passage_id, document_id, workspace_id, revision_number,
                     text, text_hash, ordinal, page_first, page_last,
                     chunk_kind, parent_chunk_id, created_at, updated_at)
                VALUES (COALESCE($9::uuid, gen_random_uuid()),
                        $1::uuid, $2::uuid, $3, $4, $5, $6,
                        $7, $8,
                        $10, $11::uuid, NOW(), NOW())
                ON CONFLICT (document_id, revision_number, text_hash) DO NOTHING
                RETURNING passage_id::text
                """,
                document_id, workspace_id, revision_number,
                c["text"], c["text_hash"], c["ordinal"],
                c["page_first"], c["page_last"],
                c.get("passage_id_override"),
                c.get("chunk_kind", "narrative"),
                c.get("parent_chunk_id"),
            )
            if r:
                inserted += 1
        except Exception as e:
            log.warning("pdf_ingester.passage_insert_failed err=%s", e)
    return inserted


async def ingest_pdf_file(
    conn: asyncpg.Connection,
    pdf_path: str,
    *,
    workspace_id: str,
    project_id: str | None = None,
    title_override: str | None = None,
) -> PDFIngestResult:
    """Ingest one PDF into silver.reports + silver.document_passages.

    Returns a PDFIngestResult.
    """
    p = Path(pdf_path)
    if not p.is_file():
        return PDFIngestResult(
            file_path=pdf_path, document_id=None, passages_inserted=0,
            page_count=0, skipped=True, skipped_reason="file_not_found",
        )

    # Compute sha first for idempotency
    pdf_bytes = p.read_bytes()
    sha = hashlib.sha256(pdf_bytes).hexdigest()

    try:
        pages = _extract_text_pages(pdf_path)
    except Exception as e:
        return PDFIngestResult(
            file_path=pdf_path, document_id=None, passages_inserted=0,
            page_count=0, skipped=True, skipped_reason="pdfminer_failed",
            error=f"{type(e).__name__}: {str(e)[:200]}",
        )

    total_chars = sum(len(p) for p in pages)
    if total_chars < _MIN_CHUNK * 2:
        # Either an empty PDF or a scanned PDF — flag for OCR follow-on
        return PDFIngestResult(
            file_path=pdf_path, document_id=None, passages_inserted=0,
            page_count=len(pages), skipped=True,
            skipped_reason="empty_or_scanned_pdf_no_native_text",
        )

    chunks = _chunk_pages(pages)
    title = title_override or p.stem

    document_id = await _get_or_create_document(
        conn,
        file_path=pdf_path,
        title=title,
        project_id=project_id,
        workspace_id=workspace_id,
        source_sha256=sha,
    )

    # Set the project-scope GUC for RLS on document_passages
    if project_id:
        await conn.execute(
            "SELECT set_config('app.project_id', $1, true)",
            project_id,
        )

    passages_inserted = await _insert_passages(
        conn,
        document_id=document_id,
        workspace_id=workspace_id,
        chunks=chunks,
    )

    return PDFIngestResult(
        file_path=pdf_path,
        document_id=document_id,
        passages_inserted=passages_inserted,
        page_count=len(pages),
    )


__all__ = ["ingest_pdf_file", "PDFIngestResult"]
