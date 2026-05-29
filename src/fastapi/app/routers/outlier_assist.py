"""LLM-assist outlier-flagging endpoint — Track A.1 Phase 4.B-ii.

POST /internal/outlier-assist
-----------------------------
Called by the Dagster outlier detector
(``georag_dagster.assets._outlier_llm.call_llm_assist``) when rule-based
percentile detection produces no flag and the row's per-record confidence
sits in the ambiguity band [0.65, 0.85]. The handler asks Qwen3-14B-AWQ
(via the vLLM OpenAI-compat client; the Ollama path is deprecated) to
assess whether any numeric value in the payload is anomalous in
geological context.

Returns the structured ``OutlierAssessment`` shape — Dagster expects
``{is_anomalous: bool, reason: str}`` exactly.

Failure modes (return 502):
  - Ollama / vLLM unreachable
  - LLM produced non-JSON output despite ``format=json``
  - LLM produced JSON missing required fields

The Dagster helper handles 502 / non-200 by returning None and falling
back to the rule-based-only result. End user impact: no outlier flag —
the row continues with whatever routing decision parser confidence
produced. LLM-assist is enrichment, not gating.

This is an INTERNAL endpoint (mounted under /internal/...). It is intended
only for ingestion-pipeline-side calls and is not exposed to the chat /
Inertia surfaces.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/outlier-assist", tags=["internal", "review-queue"])

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class OutlierAssistRequest(BaseModel):
    """Request body matching the Dagster helper's
    ``build_request_payload`` shape.
    """

    target_table: str = Field(
        ...,
        description="Fully-qualified silver table name, e.g. 'silver.collars'",
        max_length=128,
    )
    payload: dict[str, float] = Field(
        ...,
        description=(
            "Numeric fields from the parsed silver record (non-numeric fields "
            "are stripped client-side)."
        ),
    )
    peer_stats: dict[str, dict[str, float]] | None = Field(
        default=None,
        description=(
            "Optional per-field summary statistics ({field: {p50, p95, count}}). "
            "When provided the model can reason about distribution shape "
            "without re-querying the corpus."
        ),
    )


class OutlierAssistResponse(BaseModel):
    """Structured response — must match Dagster's
    ``OutlierLlmAssessment`` dataclass exactly.
    """

    is_anomalous: bool
    reason: str = Field(..., min_length=1, max_length=500)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a geological data quality assistant. Given a parsed record from a
silver-tier table and (optionally) summary statistics for the table's
peer corpus, decide whether any numeric value in the payload is anomalous
for the table's domain.

Rules:
  1. Reply with a single JSON object: {"is_anomalous": <bool>, "reason": "<string>"}
  2. Use is_anomalous=true ONLY when at least one value is implausible OR
     dramatically inconsistent with peer_stats when provided. Mild outliers
     within a reasonable range are NOT anomalous.
  3. The reason string must be 1-2 short sentences naming the suspect
     field(s) and quantifying why. Never ramble; never speculate beyond
     the values shown. Maximum 500 characters.
  4. If you cannot decide, default to is_anomalous=false. The caller has a
     rule-based fallback; an honest "no signal" beats a hallucinated one.
  5. Do not output anything outside the JSON object — no preamble, no
     trailing notes, no markdown fences. Just the JSON.

Domain context:
  - silver.collars — drill-hole metadata; depth typically 0-2000m, easting/
    northing in UTM coords, dip -90..0.
  - silver.samples — assays; commodity_assays JSONB carries ppm/ppb values
    that vary by element (U3O8 typically <0.1% = 1000 ppm).
  - silver.lithology_logs — interval depths; rqd/recovery in [0, 100].
  - silver.mineral_claims — claim polygons; jurisdiction codes are short.
  - silver.seismic_surveys — trace counts in 100s-100000s.
  - silver.drill_traces — derived from surveys; trace_quality string flags.
  - silver.raster_layers — geotiff metadata; widths/heights in pixels.
"""


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("", response_model=OutlierAssistResponse)
async def outlier_assist(
    request: Request,
    body: OutlierAssistRequest,
) -> OutlierAssistResponse:
    """LLM-assist outlier flagging via Qwen3-14B-AWQ on vLLM.

    Uses the OpenAI-compatible /v1/chat/completions endpoint of the
    configured backend (vLLM is canonical for both dev and prod; the
    Ollama path is deprecated). response_format=json_object forces the
    model to emit a single JSON object per the system prompt.
    """
    # Pull the pooled httpx client from app.state when available — same
    # connection-reuse trick the orchestrator uses. Fall back to a per-call
    # Client when not (test paths, pre-pool deploys).
    pooled = getattr(request.app.state, "llm_http_client", None)
    # Drift C-03 (test sweep 2026-05-13): effective_llm_url is a @property that
    # raises RuntimeError when LLM_BACKEND=anthropic. The previous
    # `getattr(..., default)` only catches AttributeError, so the default never
    # fires and the RuntimeError leaked into every test that didn't override
    # LLM_BACKEND. Explicit try/except is the correct shape; outlier_assist
    # always uses the OpenAI-compatible chat path, so anthropic backends fall
    # back to the configured Ollama endpoint via LLM_PRIMARY_URL.
    try:
        base_url = settings.effective_llm_url
    except RuntimeError:
        base_url = settings.LLM_PRIMARY_URL  # ollama-compat default
    model = settings.effective_llm_model

    user_message = json.dumps(body.model_dump(exclude_none=True))

    chat_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "stream": False,
        "format": "json",  # Ollama-native; vLLM ignores this and the prompt does the work.
        "options": {
            "num_predict": 256,
            "num_ctx": 4096,
            "temperature": 0.2,  # low for determinism on a classify task
        },
    }

    try:
        if pooled is not None:
            resp = await pooled.post(f"{base_url}/chat/completions", json=chat_body, timeout=15.0)
        else:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(f"{base_url}/chat/completions", json=chat_body)
    except httpx.HTTPError as exc:
        logger.warning("outlier-assist: LLM HTTP error — %s", exc)
        raise HTTPException(status_code=502, detail="llm_unreachable") from exc

    if resp.status_code != 200:
        logger.warning(
            "outlier-assist: LLM returned status=%d body=%r",
            resp.status_code, resp.text[:200],
        )
        raise HTTPException(status_code=502, detail="llm_non_200")

    try:
        chat_result = resp.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="llm_chat_invalid_json") from exc

    # Extract the assistant message content. OpenAI-compat shape:
    #   {"choices": [{"message": {"content": "<json string>"}}]}
    try:
        content = chat_result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning("outlier-assist: unexpected chat response shape: %r", chat_result)
        raise HTTPException(status_code=502, detail="llm_chat_shape_drift") from exc

    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=502, detail="llm_empty_content")

    # Parse the JSON the model emitted.
    try:
        parsed: Any = json.loads(content)
    except ValueError:
        # Be defensive: some models wrap the JSON in markdown fences or
        # trailing whitespace despite format=json. Try to extract a JSON
        # object substring.
        stripped = _extract_json_object(content)
        if stripped is None:
            raise HTTPException(status_code=502, detail="llm_response_not_json")
        try:
            parsed = json.loads(stripped)
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="llm_response_not_json") from exc

    if not isinstance(parsed, dict):
        raise HTTPException(status_code=502, detail="llm_response_not_object")

    is_anom = parsed.get("is_anomalous")
    reason = parsed.get("reason")
    if not isinstance(is_anom, bool):
        raise HTTPException(status_code=502, detail="llm_missing_is_anomalous")
    if not isinstance(reason, str) or not reason.strip():
        raise HTTPException(status_code=502, detail="llm_missing_reason")

    return OutlierAssistResponse(
        is_anomalous=is_anom,
        reason=reason.strip()[:500],
    )


def _extract_json_object(text: str) -> str | None:
    """Best-effort extraction of a single JSON object substring. Strips
    common LLM artifacts like markdown code fences."""
    s = text.strip()
    # Strip ```json ... ``` fences
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
        if s.endswith("```"):
            s = s[:-3].strip()
    # Find first { and matching }
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return s[start : end + 1]
