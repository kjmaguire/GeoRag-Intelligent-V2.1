"""§04p preflight — qpdf + pikepdf PDF normalization and integrity check.

**Master-plan §9.3 reference.** First stage in the §04p pipeline.
Verifies the source PDF is parseable, reports the basic structural
facts the profiler needs.

**Status:** Step 3 implementation (doc-phase 51). Behaviour landed.

Output schema (locked here):
    {
        "valid": bool,
        "page_count": int | None,
        "encrypted": bool,
        "sha256": str | None,
        "magic_ok": bool,
        "error": str | None,   # human-readable reason when valid=False
    }
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any


async def preflight(pdf_path: Path) -> dict[str, Any]:
    """Run qpdf + pikepdf preflight on a Bronze-tier PDF.

    Args:
        pdf_path: Local filesystem path to the source PDF.

    Returns:
        Preflight result dict (see module docstring for schema).
    """
    return await asyncio.to_thread(_preflight_sync, pdf_path)


def _preflight_sync(pdf_path: Path) -> dict[str, Any]:
    """Synchronous implementation; called via asyncio.to_thread."""
    import pikepdf

    if not pdf_path.exists():
        return {
            "valid": False,
            "page_count": None,
            "encrypted": False,
            "sha256": None,
            "magic_ok": False,
            "error": "file_not_found",
        }

    raw = pdf_path.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    magic_ok = raw[:5] == b"%PDF-"
    if not magic_ok:
        return {
            "valid": False,
            "page_count": None,
            "encrypted": False,
            "sha256": sha256,
            "magic_ok": False,
            "error": "not_a_pdf_magic_mismatch",
        }

    try:
        with pikepdf.open(pdf_path) as pdf:
            return {
                "valid": True,
                "page_count": len(pdf.pages),
                "encrypted": pdf.is_encrypted,
                "sha256": sha256,
                "magic_ok": True,
                "error": None,
            }
    except pikepdf.PasswordError:
        return {
            "valid": False,
            "page_count": None,
            "encrypted": True,
            "sha256": sha256,
            "magic_ok": True,
            "error": "encrypted_no_password",
        }
    except pikepdf.PdfError as exc:
        # pikepdf wraps qpdf's internal errors; this catches corrupt PDFs,
        # truncated files, etc. Caller routes these to silver.document_ingestion_quality
        # with recommended_action = "reject".
        return {
            "valid": False,
            "page_count": None,
            "encrypted": False,
            "sha256": sha256,
            "magic_ok": True,
            "error": f"pdf_error: {exc}",
        }
