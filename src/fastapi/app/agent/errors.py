"""Structured error types for the RAG pipeline.

Replaces generic exception strings with typed error codes and user-facing
messages so the frontend can render actionable feedback.
"""

from __future__ import annotations

import logging
from enum import StrEnum

logger = logging.getLogger(__name__)


class ErrorCode(StrEnum):
    """Structured error codes for the RAG pipeline."""
    TIMEOUT = "TIMEOUT"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    DATABASE_ERROR = "DATABASE_ERROR"
    NO_RESULTS = "NO_RESULTS"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
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
    ErrorCode.INTERNAL_ERROR: (
        "An unexpected error occurred. The team has been notified. "
        "Please try again or rephrase your question."
    ),
}


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
