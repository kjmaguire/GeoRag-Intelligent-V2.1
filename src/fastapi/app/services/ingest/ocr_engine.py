"""Which remote OCR engine ``OCR_ENGINE`` selects, with retired values called out.

Before ADR-0019 the selector was a bare string compare inside the Azure
Document Intelligence adapter, with ``"tesseract"`` as the default for
anything else. That meant a worker whose environment still said
``OCR_ENGINE=azure_document_intelligence`` after the swap would have run
Tesseract on every page — no tables, no structure — and said nothing. The
2026-08-21 NotConfigured fix exists because exactly this class of silent
downgrade already happened once; this module makes the retired value loud.

Read from ``os.environ`` at call time (not frozen at import) to match the
adapter convention, so tests can flip it with ``monkeypatch.setenv``.

Default (audit 2026-10-04): an UNSET ``OCR_ENGINE`` selects ``cohere_parse``,
the hosted engine production actually runs, the same rule ``EMBEDDING_BACKEND``
follows. This is fail-safe: with no ``COHERE_API_KEY`` the per-page ladder
(``pdf_report._ocr_single_page``) logs ONE CRITICAL per process and runs
Tesseract, and ``pdf_report._effective_parse_mode`` degrades ``PDF_PARSE_MODE``
to ``ocr_only``. ``tesseract`` stays selectable (air-gapped sites set it).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("georag.ingest.ocr_engine")

ENGINE_ENV = "OCR_ENGINE"

COHERE_PARSE = "cohere_parse"
TESSERACT = "tesseract"

#: Values that used to select an engine this pipeline no longer has.
RETIRED_VALUES: frozenset[str] = frozenset(
    {"azure_document_intelligence", "document_intelligence"}
)

_WARNED: set[str] = set()


def selected_engine() -> str:
    """``"cohere_parse"`` or ``"tesseract"`` — never anything else.

    Unset (or blank) means ``"cohere_parse"``; ``"tesseract"`` must be asked
    for explicitly. Whether the hosted engine is *configured* (``COHERE_API_KEY``)
    is the caller's question — a keyless worker falls back to Tesseract loudly
    (see ``pdf_report._ocr_single_page``). A retired value logs CRITICAL once
    per process (CRITICAL pages — georag-fastapi-critical) and runs Tesseract
    so ingestion keeps moving; an unknown value warns once and does the same.
    """
    raw = (os.environ.get(ENGINE_ENV) or "").strip().lower() or COHERE_PARSE
    if raw == COHERE_PARSE:
        return COHERE_PARSE
    if raw == TESSERACT:
        return TESSERACT
    if raw in RETIRED_VALUES:
        if raw not in _WARNED:
            _WARNED.add(raw)
            logger.critical(
                "ocr_engine: %s=%r selects Azure Document Intelligence, which was "
                "retired on 2026-09-02 (ADR-0019). EVERY scanned page now falls "
                "back to tesseract, which extracts no tables. Set %s=%s.",
                ENGINE_ENV,
                raw,
                ENGINE_ENV,
                COHERE_PARSE,
            )
        return TESSERACT
    if raw not in _WARNED:
        _WARNED.add(raw)
        logger.warning(
            "ocr_engine: %s=%r is not a known engine (%s | %s) — using tesseract",
            ENGINE_ENV,
            raw,
            COHERE_PARSE,
            TESSERACT,
        )
    return TESSERACT


# ---------------------------------------------------------------------------
# PDF_PARSE_MODE — which pages of a born-digital PDF the remote engine reads
# ---------------------------------------------------------------------------

PARSE_MODE_ENV = "PDF_PARSE_MODE"

#: The remote engine reads only the pages with no usable text layer.
#: Tesseract is the floor for those. This is what a worker degrades to when
#: the engine is not usable (OCR_ENGINE=tesseract, or no COHERE_API_KEY).
PARSE_MODE_OCR_ONLY = "ocr_only"
#: Native text stays for prose. Native pages on which a table is detected are
#: ALSO sent to the remote engine, and the engine's table grids replace the
#: pdfplumber tables for that page.
PARSE_MODE_TABLES = "tables"
#: Every page goes through the remote engine. Native text is used only as the
#: fallback when the engine cannot answer for a page.
PARSE_MODE_ALL = "all"

PARSE_MODES: frozenset[str] = frozenset(
    {PARSE_MODE_OCR_ONLY, PARSE_MODE_TABLES, PARSE_MODE_ALL}
)

_PARSE_MODE_WARNED: set[str] = set()


def selected_parse_mode() -> str:
    """``"ocr_only"``, ``"tables"`` or ``"all"`` — never anything else.

    Unset (or blank) is ``all`` — the production setting (Kyle, 2026-09-29),
    so a dev box exercises the path production runs. It is safe on a box that
    cannot run it: with ``OCR_ENGINE=tesseract`` this function degrades to
    ``ocr_only`` (warning once), and with no ``COHERE_API_KEY``
    ``pdf_report._effective_parse_mode`` degrades to ``ocr_only`` (CRITICAL
    once) — native text keeps flowing either way. The "needs OCR_ENGINE"
    warning is only logged when ``PDF_PARSE_MODE`` was set explicitly.

    ``tables`` and ``all`` only make sense when the remote engine is the one
    selected: Tesseract on a born-digital page is strictly worse than the text
    layer already in the file. With ``OCR_ENGINE=tesseract`` they warn once and
    behave as ``ocr_only``. An unknown value logs CRITICAL once (CRITICAL
    pages — georag-fastapi-critical) and behaves as ``ocr_only``: a typo must
    not be able to route every page of every document to a paid engine, or
    silently to none, without anyone being told.

    Whether the engine is *configured* (``COHERE_API_KEY``) is the caller's
    question — see ``pdf_report._effective_parse_mode``.
    """
    explicit = (os.environ.get(PARSE_MODE_ENV) or "").strip().lower()
    raw = explicit or PARSE_MODE_ALL
    if raw == PARSE_MODE_OCR_ONLY:
        return PARSE_MODE_OCR_ONLY
    if raw in PARSE_MODES:
        if selected_engine() != COHERE_PARSE:
            key = f"engine:{raw}"
            # Only an operator who typed the mode gets told it was ignored;
            # the built-in default degrading on a tesseract site is by design.
            if explicit and key not in _PARSE_MODE_WARNED:
                _PARSE_MODE_WARNED.add(key)
                logger.warning(
                    "ocr_engine: %s=%r needs %s=%s (tesseract on born-digital "
                    "pages is strictly worse than the text layer) — behaving "
                    "as %s",
                    PARSE_MODE_ENV,
                    raw,
                    ENGINE_ENV,
                    COHERE_PARSE,
                    PARSE_MODE_OCR_ONLY,
                )
            return PARSE_MODE_OCR_ONLY
        return raw
    if raw not in _PARSE_MODE_WARNED:
        _PARSE_MODE_WARNED.add(raw)
        logger.critical(
            "ocr_engine: %s=%r is not a known parse mode (%s) — behaving as %s",
            PARSE_MODE_ENV,
            raw,
            " | ".join(sorted(PARSE_MODES)),
            PARSE_MODE_OCR_ONLY,
        )
    return PARSE_MODE_OCR_ONLY


__all__ = [
    "COHERE_PARSE",
    "ENGINE_ENV",
    "PARSE_MODES",
    "PARSE_MODE_ALL",
    "PARSE_MODE_ENV",
    "PARSE_MODE_OCR_ONLY",
    "PARSE_MODE_TABLES",
    "RETIRED_VALUES",
    "TESSERACT",
    "selected_engine",
    "selected_parse_mode",
]
