"""Structured error types for the RAG pipeline.

Replaces generic exception strings with typed error codes and user-facing
messages so the frontend can render actionable feedback.
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """Structured error codes for the RAG pipeline."""
    TIMEOUT = "TIMEOUT"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    DATABASE_ERROR = "DATABASE_ERROR"
    NO_RESULTS = "NO_RESULTS"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
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
    ErrorCode.INTERNAL_ERROR: (
        "An unexpected error occurred. The team has been notified. "
        "Please try again or rephrase your question."
    ),
}


def classify_error(exc: Exception) -> tuple[ErrorCode, str]:
    """Classify an exception into a structured error code + user message."""
    import asyncio
    import httpx

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
