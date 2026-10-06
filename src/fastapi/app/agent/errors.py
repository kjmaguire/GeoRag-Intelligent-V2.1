"""Structured error types for the RAG pipeline.

Replaces generic exception strings with typed error codes and user-facing
messages so the frontend can render actionable feedback.
"""

from __future__ import annotations

import logging
import re
from enum import StrEnum

logger = logging.getLogger(__name__)


class RetrievalBackendUnavailable(RuntimeError):
    """Document retrieval could not run (audit RAG-12, 2026-09-29).

    Raised by the agentic graph's execute node when search_documents came
    back empty because a backend FAILED — sparse encoder down, Qdrant error
    — rather than because nothing matched. Answering anyway produced either
    a false "no passages cleared the relevance threshold" refusal or a
    normal-looking answer built without documents, with nothing telling
    the user. There is no dense-only fallback by design (GI-11).
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"document retrieval unavailable: {reason}")


class EmptySparseQuery(RuntimeError):
    """The query produced no sparse (SPLADE) terms, so hybrid retrieval cannot run.

    ``encode_sparse`` returns ``{}`` for text with no encodable tokens
    (symbol-only, or some non-Latin input). ``search_documents`` used to hand
    that empty vector to Qdrant, which silently turned a hybrid query into a
    dense-only one -- exactly what GI-11 forbids (audit item 27). It is a
    property of THIS QUESTION, not an outage, so it is its own error: the
    user is told to rephrase, and nothing is paged.
    """

    def __init__(self, detail: str = "query has no searchable terms") -> None:
        self.detail = detail
        super().__init__(detail)


class ErrorCode(StrEnum):
    """Structured error codes for the RAG pipeline."""
    TIMEOUT = "TIMEOUT"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    DATABASE_ERROR = "DATABASE_ERROR"
    NO_RESULTS = "NO_RESULTS"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    RETRIEVAL_UNAVAILABLE = "RETRIEVAL_UNAVAILABLE"
    QUERY_NOT_SEARCHABLE = "QUERY_NOT_SEARCHABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# User-facing messages — clear, actionable, non-technical.
USER_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.TIMEOUT: (
        "Your query took too long to process. Try a more specific question "
        "or ask about fewer drill holes at once."
    ),
    ErrorCode.LLM_UNAVAILABLE: (
        "The language model is currently unavailable. This usually resolves "
        "within a few minutes. Please try again shortly."
    ),
    ErrorCode.DATABASE_ERROR: (
        "A database connection error occurred. If this persists, contact "
        "your administrator."
    ),
    ErrorCode.NO_RESULTS: (
        "No relevant data was found for your query in this project. "
        "Check that the correct project is selected and try rephrasing."
    ),
    ErrorCode.VALIDATION_FAILED: (
        "The response could not be verified against the source data. "
        "This is a safety measure — please rephrase your question."
    ),
    ErrorCode.RATE_LIMITED: (
        "You've exceeded the query rate limit. Please wait a moment "
        "before trying again."
    ),
    # Distinct from RATE_LIMITED on purpose. Rate limiting clears by
    # waiting a moment; a workspace cost ceiling does not clear until the
    # calendar month rolls over or an administrator raises it, so telling
    # the user to "wait a moment" would be false.
    ErrorCode.QUOTA_EXCEEDED: (
        "This workspace has reached its monthly query budget, so new "
        "questions are paused. An administrator can raise the limit; "
        "otherwise it resets at the start of next month."
    ),
    ErrorCode.RETRIEVAL_UNAVAILABLE: (
        "Document search is temporarily unavailable, so this question could "
        "not be checked against your reports. Please try again in a few "
        "minutes."
    ),
    ErrorCode.QUERY_NOT_SEARCHABLE: (
        "This question has no searchable words in it, so the documents "
        "could not be searched. Please rephrase it in plain words, for "
        "example with a hole ID, a commodity or a place name."
    ),
    ErrorCode.INTERNAL_ERROR: (
        "An unexpected error occurred. The team has been notified. "
        "Please try again or rephrase your question."
    ),
}

_HTTP_STATUS_RE = re.compile(r"\bHTTP (\d{3})\b")


def _provider_error_code(exc: Exception) -> ErrorCode | None:
    """Model-provider failures that carry a meaning the user can act on.

    Audit AGT-14: CoherePreStreamError / CohereResponseShapeError were
    RuntimeErrors whose text matched no branch below, so a provider throttle
    told the user "An unexpected error occurred. The team has been notified."

    Bedrock is classified by the botocore ``ClientError`` it re-raises once its
    pre-stream retries are spent (``llm_bedrock._is_transient``): the adapter
    never raised a wrapper type, so the ``BedrockPreStreamError`` this used to
    look for could never match, and a Bedrock 5xx/model-timeout reported as
    INTERNAL_ERROR.
    """
    try:
        from app.agent.llm_cohere import (  # noqa: PLC0415
            CoherePreStreamError,
            CohereResponseShapeError,
        )

        if isinstance(exc, (CoherePreStreamError, CohereResponseShapeError)):
            status = getattr(exc, "status_code", None)
            if status is None:
                m = _HTTP_STATUS_RE.search(str(exc))
                status = int(m.group(1)) if m else None
            if status == 429 or "throttl" in str(exc).lower():
                return ErrorCode.RATE_LIMITED
            return ErrorCode.LLM_UNAVAILABLE
    except ImportError:  # pragma: no cover — adapter always importable in app
        logger.debug("classify_error: llm_cohere unavailable", exc_info=True)
    try:
        from app.agent.llm_bedrock import _error_code, _is_transient  # noqa: PLC0415

        if _is_transient(exc):
            if _error_code(exc) == "ThrottlingException":
                return ErrorCode.RATE_LIMITED
            return ErrorCode.LLM_UNAVAILABLE
    except ImportError:  # pragma: no cover
        logger.debug("classify_error: llm_bedrock unavailable", exc_info=True)
    return None


def classify_error(exc: Exception) -> tuple[ErrorCode, str]:
    """Classify an exception into a structured error code + user message."""
    import asyncio

    import httpx

    # Checked before the generic branches: WorkspaceQuotaExceeded is a
    # RuntimeError, so without this it fell through every isinstance test to
    # INTERNAL_ERROR -- "an unexpected error occurred, the team has been
    # notified" -- for a deliberate, configured, administrator-controlled
    # stop. Imported lazily because app.agent.llm_calls imports heavily and
    # this module is deliberately cheap.
    try:
        from app.agent.llm_calls import WorkspaceQuotaExceeded  # noqa: PLC0415

        if isinstance(exc, WorkspaceQuotaExceeded):
            return ErrorCode.QUOTA_EXCEEDED, USER_MESSAGES[ErrorCode.QUOTA_EXCEEDED]
    except ImportError:  # pragma: no cover - llm_calls always importable in app
        # Not silent: this runs inside somebody else's except block, so
        # raising here would replace the error being classified with an
        # import error and lose the original entirely. Degrading to
        # INTERNAL_ERROR is the right behaviour; saying so is what stops
        # the next person wondering why a quota stop was reported as a
        # mystery.
        logger.debug(
            "classify_error: app.agent.llm_calls unavailable, so the "
            "WorkspaceQuotaExceeded branch was skipped for %s",
            type(exc).__name__,
            exc_info=True,
        )

    if isinstance(exc, EmptySparseQuery):
        return (
            ErrorCode.QUERY_NOT_SEARCHABLE,
            USER_MESSAGES[ErrorCode.QUERY_NOT_SEARCHABLE],
        )

    if isinstance(exc, RetrievalBackendUnavailable):
        return (
            ErrorCode.RETRIEVAL_UNAVAILABLE,
            USER_MESSAGES[ErrorCode.RETRIEVAL_UNAVAILABLE],
        )

    provider_code = _provider_error_code(exc)
    if provider_code is not None:
        return provider_code, USER_MESSAGES[provider_code]

    if isinstance(exc, asyncio.TimeoutError):
        return ErrorCode.TIMEOUT, USER_MESSAGES[ErrorCode.TIMEOUT]

    if isinstance(exc, httpx.HTTPError):
        return ErrorCode.LLM_UNAVAILABLE, USER_MESSAGES[ErrorCode.LLM_UNAVAILABLE]

    if isinstance(exc, (ConnectionError, OSError)):
        return ErrorCode.DATABASE_ERROR, USER_MESSAGES[ErrorCode.DATABASE_ERROR]

    exc_str = str(exc).lower()
    if "connection refused" in exc_str or "connection reset" in exc_str:
        return ErrorCode.DATABASE_ERROR, USER_MESSAGES[ErrorCode.DATABASE_ERROR]

    if "rate limit" in exc_str or "throttl" in exc_str:
        return ErrorCode.RATE_LIMITED, USER_MESSAGES[ErrorCode.RATE_LIMITED]

    return ErrorCode.INTERNAL_ERROR, USER_MESSAGES[ErrorCode.INTERNAL_ERROR]
