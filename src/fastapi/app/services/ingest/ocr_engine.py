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

    Unset and ``"tesseract"`` both mean the local engine. A retired value
    logs CRITICAL once per process (CRITICAL pages — georag-fastapi-critical)
    and runs Tesseract so ingestion keeps moving; an unknown value warns
    once and does the same.
    """
    raw = (os.environ.get(ENGINE_ENV) or TESSERACT).strip().lower()
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


__all__ = [
    "COHERE_PARSE",
    "ENGINE_ENV",
    "RETIRED_VALUES",
    "TESSERACT",
    "selected_engine",
]
