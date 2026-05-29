"""Stage 1 — PDF preflight via qpdf (pikepdf binding).

§04p pipeline Stage 1 responsibilities:
  - Validate PDF structure (pikepdf opens and inspects).
  - Reject encrypted PDFs immediately (raise PdfEncryptedError).
  - Attempt structural repair on damaged PDFs (pikepdf save handles this
    transparently via libqpdf's repair pass).
  - Linearize the output (web-optimised, in-order byte layout).
  - If page_count > 500, report the split ranges (1–500, 501–N …).
    Phase 1.A does NOT write separate Bronze objects for each chunk —
    chunked storage is a follow-up task for Phase 1.B or later.
  - Compute pdf_id = SHA-256 hex of the ORIGINAL input bytes (pre-repair).
    This is the stable Bronze archive key.

Threading model
---------------
pikepdf is synchronous (libqpdf is a C++ library without an async interface).
All pikepdf calls are delegated to asyncio.to_thread() so they run on a
threadpool worker and do not block the FastAPI event loop.

Note: pypdfium2 (Stage 2) uses process workers instead of threads because
PDFium is not fully thread-safe.  pikepdf's libqpdf IS thread-safe (each
Pdf object is independent), so threads are sufficient here.

Extension points for Phase 1.B
-------------------------------
The normalised bytes returned by ``preflight`` can be passed directly to
Stage 2 (PdfRenderService.render_page) without re-opening from disk.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class PdfPreflightError(Exception):
    """Base class for all preflight failures."""


class PdfEncryptedError(PdfPreflightError):
    """Raised when the PDF is password-protected / encrypted.

    Encrypted PDFs cannot be processed without a password; the caller must
    obtain the password from the document owner before re-submitting.
    """


class PdfCorruptError(PdfPreflightError):
    """Raised when the PDF is structurally corrupt and cannot be repaired.

    pikepdf / libqpdf attempts automatic repair during open().  If the file
    is so damaged that even the repair pass fails, this exception is raised.
    """


# ---------------------------------------------------------------------------
# Page-split helper
# ---------------------------------------------------------------------------


def _compute_split_ranges(page_count: int, chunk_size: int = 500) -> list[tuple[int, int]]:
    """Return [(first, last), …] 1-indexed split ranges for oversized PDFs.

    Example: 600 pages → [(1, 500), (501, 600)].
    Returns an empty list when page_count <= chunk_size.
    """
    if page_count <= chunk_size:
        return []
    ranges: list[tuple[int, int]] = []
    start = 1
    while start <= page_count:
        end = min(start + chunk_size - 1, page_count)
        ranges.append((start, end))
        start = end + 1
    return ranges


# ---------------------------------------------------------------------------
# Synchronous core — runs inside asyncio.to_thread()
# ---------------------------------------------------------------------------


def _preflight_sync(pdf_bytes: bytes) -> tuple[bytes, object]:  # object = PreflightReport
    """Synchronous preflight implementation.  Called via asyncio.to_thread.

    Returns (normalised_pdf_bytes, PreflightReport).

    Raises
    ------
    PdfEncryptedError  — PDF is password-protected.
    PdfCorruptError    — PDF is so damaged it cannot be repaired.
    """
    # Import here (not at module top) so the module can be imported in
    # environments where pikepdf is not yet installed without crashing.
    try:
        import pikepdf  # noqa: PLC0415
    except ImportError as exc:
        raise PdfPreflightError(
            "pikepdf is not installed. Run: uv pip install 'pikepdf>=9.0'"
        ) from exc

    # Stable archive key = SHA-256 of the original (pre-repair) bytes.
    pdf_id = hashlib.sha256(pdf_bytes).hexdigest()

    was_repaired = False
    was_encrypted = False  # Always False on success path (encrypted → raises).

    # --- Open + validate -------------------------------------------------------
    try:
        pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    except pikepdf.PasswordError as exc:
        # pikepdf raises PasswordError when the PDF requires a password.
        raise PdfEncryptedError(
            f"PDF is password-protected (pdf_id={pdf_id[:16]}…). "
            "Cannot process without the owner password."
        ) from exc
    except pikepdf.PdfError as exc:
        # libqpdf attempts repair automatically; if it still fails, raise.
        # Distinguish: "repaired with warnings" (exc not raised) vs total
        # failure (exc is raised).  A total failure lands here.
        raise PdfCorruptError(
            f"PDF is corrupt and could not be repaired (pdf_id={pdf_id[:16]}…): {exc}"
        ) from exc

    # Check for encryption at the metadata level (some PDFs are "open" but
    # still flag as encrypted, e.g. restriction-only permissions).
    if pdf.is_encrypted:
        pdf.close()
        raise PdfEncryptedError(
            f"PDF is encrypted (pdf_id={pdf_id[:16]}…). Cannot process."
        )

    page_count = len(pdf.pages)

    # --- Detect structural repair ---------------------------------------------
    # pikepdf silently repairs during open().  We heuristically detect this by
    # comparing a round-trip save: if the saved bytes differ from input, repair
    # occurred (or at minimum the PDF was not already in canonical qpdf form).
    try:
        probe_buf = io.BytesIO()
        pdf.save(probe_buf)
        was_repaired = probe_buf.getvalue() != pdf_bytes
    except Exception:
        was_repaired = False  # conservative; log below if needed

    # --- Linearize + write output --------------------------------------------
    out_buf = io.BytesIO()
    try:
        pdf.save(out_buf, linearize=True)
    except Exception as exc:
        pdf.close()
        raise PdfCorruptError(
            f"PDF linearization failed (pdf_id={pdf_id[:16]}…): {exc}"
        ) from exc

    pdf.close()
    normalised_bytes = out_buf.getvalue()

    # --- Split ranges (report only — no chunked storage in Phase 1.A) --------
    split_ranges = _compute_split_ranges(page_count)
    if split_ranges:
        logger.info(
            "PDF oversized: pdf_id=%s page_count=%d → %d chunks reported (not yet stored separately)",
            pdf_id[:16],
            page_count,
            len(split_ranges),
        )

    # Import model here to keep circular-import risk minimal.
    from app.models.pdf import PreflightReport  # noqa: PLC0415

    try:
        qpdf_version = pikepdf.__version__
    except AttributeError:
        qpdf_version = "unknown"

    report = PreflightReport(
        pdf_id=pdf_id,
        original_bytes_hash=pdf_id,
        page_count=page_count,
        was_repaired=was_repaired,
        was_linearized=True,
        was_encrypted=was_encrypted,
        split_into_chunks=split_ranges,
        qpdf_version=qpdf_version,
        preflight_timestamp=datetime.now(UTC),
    )

    return normalised_bytes, report


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------


async def preflight(pdf_bytes: bytes) -> tuple[bytes, object]:  # object = PreflightReport
    """Run Stage 1 preflight on raw PDF bytes.

    Delegates the synchronous pikepdf work to asyncio.to_thread() so the
    FastAPI event loop is not blocked.

    Parameters
    ----------
    pdf_bytes:
        Raw bytes of the original PDF file (before any processing).

    Returns
    -------
    (normalised_bytes, PreflightReport)
        normalised_bytes — linearised, repaired PDF ready for Stage 2.
        report           — PreflightReport with metadata for Bronze storage.

    Raises
    ------
    PdfEncryptedError
        PDF is password-protected.  Cannot process without owner password.
    PdfCorruptError
        PDF is structurally corrupt and pikepdf could not repair it.
    PdfPreflightError
        Base class for the above; also raised if pikepdf is not installed.
    """
    return await asyncio.to_thread(_preflight_sync, pdf_bytes)
