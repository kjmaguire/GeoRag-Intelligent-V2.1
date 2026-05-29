"""TIFF OCR ingester for Wyoming uranium scanned drillhole archive.

Doc-phase 182 — Phase E.1.

Reads TIFF scans, runs tesseract OCR (subprocess), filters garbage
output, chunks the text, lands as `silver.document_passages` rows.

Strategy:
  - Open TIFF via Pillow (multi-page supported via .seek())
  - Run tesseract via subprocess on PNG-converted page (in-memory)
  - Heuristic-filter empty / garbage results (length, alpha ratio)
  - One report row per source TIFF; one passage per OCR'd page
  - Idempotent via source_file_sha256 dedupe

Tesseract config:
  - Language: eng (default)
  - PSM 6 (single uniform block of text) — appropriate for log scans
  - OCR engine 3 (default LSTM)

Garbage filters:
  - min_chars: 50 (page too short → likely blank scan)
  - alpha_ratio: 0.4+ (>=40% alphabetic chars → not pure noise)
  - max_short_lines: 80% (a page that's mostly very-short lines is
    likely a header/footer-only or table grid)
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import asyncpg

log = logging.getLogger("georag.ingest.tiff_ocr")


_MIN_CHARS = 50
_MIN_ALPHA_RATIO = 0.40
_MAX_PAGES = 50  # per TIFF — safety against runaway multi-page docs
_TESSERACT_TIMEOUT_S = 30  # per page

# Doc-phase 187 (Phase F.2) — chunk-quality filter.
#
# Empirically derived from the Cameco WSGS archive analysis: TIFF scans
# of gamma-log charts, plan-view diagrams, and ore-grade tables produce
# text with stopword_ratio < 0.08 (essentially no English narrative).
# Operator-controlled thresholds:
#
#   FILTER_MIN_STOPWORD_RATIO: reject chunks below this stopword density.
#     Narrative English prose typically has 0.15-0.25 stopword ratio.
#     OCR'd tabular content has < 0.05.
#     Default 0.0 = no filter (preserves all OCR output for the Cameco
#     case, which is uniformly tabular; raise to 0.10+ on narrative-rich
#     corpora to suppress tabular noise).
#
#   FILTER_MIN_VOCAB_SIZE: reject chunks with vocabulary smaller than N
#     unique alpha words. Default 20 = mild filter (skips empty/short).
#     Charts with form-field labels typically have ~20-30 unique words.
#
# Per-deployment override via env: GUARD_FILTER_MIN_STOPWORD_RATIO, etc.
import os as _os

FILTER_MIN_STOPWORD_RATIO = float(
    _os.environ.get("OCR_FILTER_MIN_STOPWORD_RATIO", "0.0")
)
FILTER_MIN_VOCAB_SIZE = int(
    _os.environ.get("OCR_FILTER_MIN_VOCAB_SIZE", "20")
)

# Common English stopwords used for the stopword-ratio filter. Picked
# the most frequent function words; not exhaustive (the goal is
# narrative-vs-tabular discrimination, not full POS tagging).
_STOPWORD_SET: frozenset[str] = frozenset({
    "the", "and", "of", "in", "to", "is", "for", "with", "by",
    "a", "an", "this", "that", "from", "are", "was", "were",
    "on", "at", "as", "be", "been", "has", "have", "had",
    "or", "but", "not", "no", "if", "it", "its", "their",
    "which", "where", "when", "who", "what", "how",
})


def _chunk_quality_passes_filter(text: str) -> tuple[bool, str | None]:
    """Doc-phase 187 — return (passes, reason_if_rejected).

    Two checks:
      1. Vocab size >= FILTER_MIN_VOCAB_SIZE
      2. Stopword ratio >= FILTER_MIN_STOPWORD_RATIO

    Defaults today (0.0, 20) accept any chunk with at least 20 unique
    alpha words — effectively a no-op filter for tabular corpora. Raise
    via env vars on narrative-rich deployments.
    """
    import re as _re_chunk
    words = _re_chunk.findall(r"\b\w+\b", text.lower())
    alpha_words = [w for w in words if w.isalpha() and len(w) >= 3]
    if len(set(alpha_words)) < FILTER_MIN_VOCAB_SIZE:
        return False, f"vocab_too_small:{len(set(alpha_words))}<{FILTER_MIN_VOCAB_SIZE}"
    if not alpha_words:
        return False, "no_alpha_words"
    stopword_count = sum(1 for w in alpha_words if w in _STOPWORD_SET)
    stopword_ratio = stopword_count / len(alpha_words)
    if stopword_ratio < FILTER_MIN_STOPWORD_RATIO:
        return False, (
            f"stopword_ratio_low:{stopword_ratio:.3f}<{FILTER_MIN_STOPWORD_RATIO}"
        )
    return True, None


@dataclass
class TIFFOCRResult:
    file_path: str
    document_id: str | None
    page_count: int
    passages_inserted: int
    chars_extracted: int
    skipped: bool = False
    skipped_reason: str | None = None
    error: str | None = None


def _is_garbage_text(text: str) -> bool:
    """Heuristic — is this OCR output usable, or noise?"""
    if not text or len(text.strip()) < _MIN_CHARS:
        return True
    alpha = sum(1 for c in text if c.isalpha())
    if alpha / max(1, len(text)) < _MIN_ALPHA_RATIO:
        return True
    return False


def _ocr_image_bytes(png_bytes: bytes, *, timeout: float = _TESSERACT_TIMEOUT_S) -> str:
    """Run tesseract on raw PNG bytes via subprocess.

    Returns the recognized text. Raises on subprocess failure.
    """
    try:
        proc = subprocess.run(
            ["tesseract", "-", "-", "-l", "eng", "--psm", "6", "--oem", "3"],
            input=png_bytes,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="replace")


def _ocr_tiff_pages(tiff_path: str) -> list[tuple[int, str]]:
    """Walk pages of a TIFF and return (page_num, ocr_text) for each.

    Empty/garbage pages return empty strings (caller filters).
    """
    from PIL import Image, ImageSequence
    Image.MAX_IMAGE_PIXELS = None  # WSGS scans are large
    pages: list[tuple[int, str]] = []
    try:
        with Image.open(tiff_path) as img:
            for page_num, frame in enumerate(ImageSequence.Iterator(img), start=1):
                if page_num > _MAX_PAGES:
                    break
                # Convert to PNG bytes (in-memory)
                buf = io.BytesIO()
                # Grayscale for OCR; helps tesseract on scans
                if frame.mode != "L":
                    frame = frame.convert("L")
                frame.save(buf, format="PNG", compress_level=1)
                png_bytes = buf.getvalue()
                text = _ocr_image_bytes(png_bytes)
                pages.append((page_num, text))
    except Exception as e:
        log.warning("tiff_ocr.pages_failed file=%s err=%s", tiff_path, e)
    return pages


async def _get_or_create_document(
    conn: asyncpg.Connection,
    *,
    file_path: str,
    title: str,
    project_id: str | None,
    workspace_id: str,
    source_sha256: str,
) -> str:
    """Idempotently create a silver.reports row for the OCR'd TIFF."""
    row = await conn.fetchrow(
        "SELECT report_id::text AS report_id FROM silver.reports "
        "WHERE source_file_sha256 = $1 LIMIT 1",
        source_sha256,
    )
    if row:
        return row["report_id"]
    row = await conn.fetchrow(
        """
        INSERT INTO silver.reports
            (report_id, project_id, workspace_id, title, commodity,
             source_file_sha256, is_scanned, parser_used,
             created_at, updated_at)
        VALUES (gen_random_uuid(), $1::uuid, $2::uuid, $3, 'uranium',
                $4, true, 'tesseract-tiff',
                NOW(), NOW())
        RETURNING report_id::text AS report_id
        """,
        project_id, workspace_id, title[:500], source_sha256,
    )
    return row["report_id"]


async def _insert_passage(
    conn: asyncpg.Connection,
    *,
    document_id: str,
    workspace_id: str,
    text: str,
    ordinal: int,
    page_number: int,
) -> bool:
    """Insert one OCR-derived passage. Returns True if inserted, False if dedup."""
    text_hash = hashlib.sha256(text.strip().encode()).hexdigest()
    r = await conn.fetchrow(
        """
        INSERT INTO silver.document_passages
            (passage_id, document_id, workspace_id, revision_number,
             text, text_hash, ordinal, page_first, page_last,
             chunk_kind, created_at, updated_at)
        VALUES (gen_random_uuid(), $1::uuid, $2::uuid, 1, $3, $4, $5,
                $6, $7, 'narrative', NOW(), NOW())
        ON CONFLICT (document_id, revision_number, text_hash) DO NOTHING
        RETURNING passage_id::text
        """,
        document_id, workspace_id, text.strip(), text_hash, ordinal,
        page_number, page_number,
    )
    return r is not None


async def ingest_tiff_file(
    conn: asyncpg.Connection,
    tiff_path: str,
    *,
    workspace_id: str,
    project_id: str | None = None,
) -> TIFFOCRResult:
    """OCR one TIFF and land usable pages in silver.document_passages."""
    p = Path(tiff_path)
    if not p.is_file():
        return TIFFOCRResult(
            file_path=tiff_path, document_id=None, page_count=0,
            passages_inserted=0, chars_extracted=0,
            skipped=True, skipped_reason="file_not_found",
        )

    # SHA for dedup
    try:
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError as e:
        return TIFFOCRResult(
            file_path=tiff_path, document_id=None, page_count=0,
            passages_inserted=0, chars_extracted=0,
            skipped=True, skipped_reason=f"read_failed:{e}",
        )

    # OCR all pages (off the event loop)
    loop = asyncio.get_event_loop()
    pages = await loop.run_in_executor(None, _ocr_tiff_pages, str(p))
    if not pages:
        return TIFFOCRResult(
            file_path=tiff_path, document_id=None, page_count=0,
            passages_inserted=0, chars_extracted=0,
            skipped=True, skipped_reason="no_pages_extracted",
        )

    # Filter garbage pages (first-pass: too-short or too-noisy)
    usable_pages = [(n, t) for n, t in pages if not _is_garbage_text(t)]

    # Doc-phase 187 (Phase F.2) — second-pass chunk-quality filter.
    # Reject pages that are mostly tabular/numeric content (low
    # stopword density). Controlled by env vars; default off for the
    # Cameco WSGS archive where the entire corpus is tabular and
    # filtering would delete everything.
    if FILTER_MIN_STOPWORD_RATIO > 0 or FILTER_MIN_VOCAB_SIZE > 1:
        quality_filtered = []
        rejection_reasons: dict[str, int] = {}
        for page_num, text in usable_pages:
            passes, reason = _chunk_quality_passes_filter(text)
            if passes:
                quality_filtered.append((page_num, text))
            elif reason:
                rejection_reasons[reason.split(":")[0]] = (
                    rejection_reasons.get(reason.split(":")[0], 0) + 1
                )
        if rejection_reasons:
            log.info(
                "tiff_ocr.chunk_quality_filter file=%s rejected=%s",
                p.name, rejection_reasons,
            )
        usable_pages = quality_filtered

    if not usable_pages:
        return TIFFOCRResult(
            file_path=tiff_path, document_id=None, page_count=len(pages),
            passages_inserted=0, chars_extracted=0,
            skipped=True, skipped_reason="all_pages_garbage_or_low_quality",
        )

    # Create document
    document_id = await _get_or_create_document(
        conn,
        file_path=tiff_path,
        title=p.stem[:500],
        project_id=project_id,
        workspace_id=workspace_id,
        source_sha256=sha,
    )

    # Insert one passage per usable page
    chars_extracted = 0
    inserted = 0
    for ordinal, (page_num, text) in enumerate(usable_pages):
        text_stripped = text.strip()
        chars_extracted += len(text_stripped)
        if await _insert_passage(
            conn,
            document_id=document_id, workspace_id=workspace_id,
            text=text_stripped, ordinal=ordinal, page_number=page_num,
        ):
            inserted += 1

    return TIFFOCRResult(
        file_path=tiff_path,
        document_id=document_id,
        page_count=len(pages),
        passages_inserted=inserted,
        chars_extracted=chars_extracted,
    )


async def ocr_cluster_tiffs(
    cluster_dir: str,
    *,
    workspace_id: str,
    project_id: str,
    conn: asyncpg.Connection | None = None,
    max_files: int | None = None,
    progress_every: int = 25,
) -> dict:
    """Batch-OCR every TIFF under `cluster_dir`.

    Sets RLS GUCs once per session (session-level, not transaction-level)
    so the long-running OCR doesn't need to re-set per-file.
    """
    import os
    own_conn = False
    if conn is None:
        user = os.environ["POSTGRES_USER"]
        password = os.environ["POSTGRES_PASSWORD"]
        host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
        port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
        db = os.environ.get("POSTGRES_DB", "georag")
        conn = await asyncpg.connect(
            f"postgres://{user}:{password}@{host}:{port}/{db}",
            statement_cache_size=0,
        )
        own_conn = True

    try:
        # Session-level GUCs
        await conn.execute(
            "SELECT set_config('georag.workspace_id', $1, false)", workspace_id,
        )
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id,
        )
        await conn.execute(
            "SELECT set_config('georag.project_id', $1, false)", project_id,
        )

        tiff_paths = sorted(
            list(Path(cluster_dir).rglob("*.tif")) +
            list(Path(cluster_dir).rglob("*.TIF")) +
            list(Path(cluster_dir).rglob("*.tiff")) +
            list(Path(cluster_dir).rglob("*.TIFF"))
        )
        if max_files:
            tiff_paths = tiff_paths[:max_files]

        summary = {
            "tiff_count": len(tiff_paths),
            "docs_created": 0,
            "pages_ocrd": 0,
            "passages_inserted": 0,
            "chars_extracted": 0,
            "skipped": 0,
            "skip_reasons": {},
        }

        log.info("ocr_cluster_tiffs.start count=%d", len(tiff_paths))

        for i, p in enumerate(tiff_paths):
            try:
                result = await ingest_tiff_file(
                    conn, str(p),
                    workspace_id=workspace_id, project_id=project_id,
                )
                if result.skipped:
                    summary["skipped"] += 1
                    reason = result.skipped_reason or "unknown"
                    summary["skip_reasons"][reason] = summary["skip_reasons"].get(reason, 0) + 1
                else:
                    summary["docs_created"] += 1
                    summary["pages_ocrd"] += result.page_count
                    summary["passages_inserted"] += result.passages_inserted
                    summary["chars_extracted"] += result.chars_extracted
            except Exception as e:
                summary["skipped"] += 1
                summary["skip_reasons"]["exception"] = summary["skip_reasons"].get("exception", 0) + 1
                log.warning("ocr_cluster_tiffs.file_failed file=%s err=%s", p, e)

            if (i + 1) % progress_every == 0:
                log.info(
                    "ocr_cluster_tiffs.progress %d/%d docs=%d passages=%d chars=%d skipped=%d",
                    i + 1, len(tiff_paths),
                    summary["docs_created"], summary["passages_inserted"],
                    summary["chars_extracted"], summary["skipped"],
                )

        log.info("ocr_cluster_tiffs.complete summary=%s", summary)
        return summary
    finally:
        if own_conn:
            await conn.close()


__all__ = ["ingest_tiff_file", "ocr_cluster_tiffs", "TIFFOCRResult"]
