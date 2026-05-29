"""Stage 6 — Vision-language section summarisation via Qwen-VL.

§04p Phase 1.D responsibilities:
  - Accept a section_ref (page, page_range, or layout_region) and render the
    corresponding pages at 200 DPI via PdfRenderService.
  - Build a multipart OpenAI-compatible chat-completions request with one
    base64-encoded PNG content part per page.
  - POST to the configured VL backend (Ollama dev / vLLM prod / Anthropic
    hosted-fallback) and validate the structured JSON response via
    VlSummaryShape Pydantic model.
  - Enforce the §04i Citation completeness guard: every claim in the response
    MUST carry a (page, bbox) tuple grounding it in the PDF.  VlSummaryShape
    validation rejects responses that fail this invariant.
  - Cache results durably in silver.pdf_vl_summaries keyed on
    (pdf_id, section_ref_hash, model_id).  Different model versions are
    cached independently.

Threading model — ASYNC I/O only
---------------------------------
VL inference is I/O-bound (httpx POST to an out-of-process LLM backend).
No ProcessPoolExecutor is needed — the asyncio event loop parks the coroutine
while httpx waits for the response.  This is intentionally different from the
Stages 3–5 services (pdfminer / pdfplumber / PaddleOCR) which are CPU-bound
and require process workers.

Operator action required
------------------------
Before using the /pdf/summarize_section endpoint the Qwen2.5-VL model must
be served by vLLM:

  vLLM (dev + prod):
    python -m vllm.entrypoints.openai.api_server \\
        --model Qwen/Qwen2.5-VL-7B-Instruct \\
        --max-model-len 32768

  vLLM serves the model on the OpenAI-compatible /v1/chat/completions path
  with multipart image content.

Config env vars (all optional — defaults work for the in-network vllm service):
  PDF_VL_MODEL_ID        — model identifier (default: "Qwen/Qwen2.5-VL-7B-Instruct")
  PDF_VL_BACKEND         — "vllm" | "anthropic" (default: "vllm")
  PDF_VL_BACKEND_URL     — full base URL (default: "http://vllm:8000/v1")
  PDF_VL_TIMEOUT_S       — seconds to wait for VL inference (default: 120)
  PDF_VL_MAX_PAGES       — maximum pages per summarize request (default: 4)

Lifespan integration
--------------------
PdfVlService is a singleton held on app.state.pdf_vl_service.
Initialise it in the FastAPI lifespan startup hook after the asyncpg pool and
the render service:

    app.state.pdf_vl_service = PdfVlService(
        pool=app.state.pg_pool,
        render_service=app.state.pdf_render_service,
        http_client=getattr(app.state, "openai_http_client", None),
    )

No explicit shutdown is needed — PdfVlService owns no worker pool.

Cache-on-summarize pattern
--------------------------
summarize_section() follows the same pattern as the Stage 3–5 services:
  1. Compute section_ref_hash = sha256(json.dumps(section_ref, sort_keys=True)).
  2. Check silver.pdf_vl_summaries for an existing cache hit.
  3. On miss: resolve pages → render PNGs → call VL backend → validate output.
  4. Persist to silver.pdf_vl_summaries.
  5. Return (result_dict, cache_hit).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    import asyncpg

    from app.services.pdf_render import PdfRenderService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration (read from environment at construction time)
# ---------------------------------------------------------------------------

_DEFAULT_MODEL_ID = "Qwen/Qwen2.5-VL-7B-Instruct"
_DEFAULT_BACKEND = "vllm"
_DEFAULT_BACKEND_URL = "http://vllm:8000/v1"
_DEFAULT_TIMEOUT_S = 120.0
_DEFAULT_MAX_PAGES = 4


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class VlSectionTooLargeError(Exception):
    """Raised when the section_ref resolves to more pages than PDF_VL_MAX_PAGES.

    The caller (router) converts this to a 422 response with
    {"detail": "section_too_large", "max_pages": <int>}.
    """

    def __init__(self, page_count: int, max_pages: int) -> None:
        self.page_count = page_count
        self.max_pages = max_pages
        super().__init__(
            f"Section spans {page_count} pages; maximum is {max_pages}. "
            "Use a narrower section_ref (page or page_range with fewer pages)."
        )


class VlBackendError(Exception):
    """Raised when the VL backend returns a non-200 status or is unreachable.

    The caller (router) converts this to a 502 response with
    {"detail": "vl_backend_error"}.
    """

    def __init__(self, status_code: int | None, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"VL backend error (status={status_code}): {detail}")


class VlOutputShapeError(Exception):
    """Raised when the LLM response does not validate against VlSummaryShape.

    The caller (router) converts this to a 502 response with
    {"detail": "vl_output_shape_error"}.  The LLM is the upstream that
    misbehaved — hence 502 rather than 422.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"VL output shape validation failed: {reason}")


# ---------------------------------------------------------------------------
# Pydantic output-validation models
# (also re-exported via app.models.pdf for the router and test imports)
# ---------------------------------------------------------------------------


class VlClaim(BaseModel):
    """A single factual claim grounded to a (page, bbox) location.

    Every claim in a VlSummaryShape.claims list must carry (page, bbox) so
    the §04p (pdf_id, page, bbox) provenance contract is satisfiable.
    pdf_id is implicit from the parent summarize_section() call.

    bbox is [x0, y0, x1, y1] in PDF user-space coordinates
    (origin = bottom-left, y increases upward, units = points).
    """

    claim_text: str = Field(..., min_length=1, description="Verbatim factual claim from the summary")
    page: int = Field(..., ge=1, description="1-indexed page number where the claim is visible")
    bbox: tuple[float, float, float, float] = Field(
        ...,
        description="[x0, y0, x1, y1] in PDF user-space points (bottom-left origin, y-up)",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="LLM self-reported confidence that this claim is accurately grounded [0.0, 1.0]",
    )


class VlSummaryShape(BaseModel):
    """Shape expected from the VL model's JSON response.

    Pydantic validation here is the §04i Citation completeness guard:
    any claim that lacks a (page, bbox) fails validation and triggers
    VlOutputShapeError — the LLM response is rejected, not accepted with
    partial provenance.
    """

    summary: str = Field(..., min_length=1, description="Natural-language summary of the section")
    claims: list[VlClaim] = Field(
        default_factory=list,
        description=(
            "Per-claim provenance list.  Every factual statement in `summary` "
            "must appear here with a (page, bbox) grounding tuple."
        ),
    )

    @field_validator("claims", mode="before")
    @classmethod
    def claims_must_be_list(cls, v: Any) -> Any:
        if not isinstance(v, list):
            raise ValueError("claims must be a list")
        return v


# ---------------------------------------------------------------------------
# System prompt for Qwen-VL
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a geological document analyzer. Given page images from a PDF geological
report, produce a structured JSON summary of the section shown.

Output format (JSON only — no markdown fences, no preamble, no trailing text):
{
  "summary": "<concise natural-language summary of the section, 2–5 sentences>",
  "claims": [
    {
      "claim_text": "<verbatim factual claim visible in the images>",
      "page": <1-indexed page number where this claim appears>,
      "bbox": [<x0>, <y0>, <x1>, <y1>],
      "confidence": <float 0.0–1.0>
    }
  ]
}

Rules:
  1. Every factual statement in `summary` MUST appear in `claims` with a
     (page, bbox) tuple identifying where it is visible in the images.
  2. bbox values are in PDF user-space coordinates (origin bottom-left, y-up,
     units points).  Estimate them from the visual position on the page image
     at 200 DPI (72 points per inch, 200/72 ≈ 2.78 pixels per point).
  3. Do NOT invent claims that are not visible in the images.
  4. If you cannot ground a claim to a specific location, omit it entirely
     rather than guessing.
  5. confidence reflects how certain you are that the claim is accurately
     transcribed from the visible text (1.0 = clearly legible, 0.5 = partially
     obscured, 0.0 = inferred/uncertain).
  6. Return JSON only. No markdown, no explanation outside the JSON object.
"""


# ---------------------------------------------------------------------------
# JSON extraction helper (mirrors outlier_assist._extract_json_object)
# ---------------------------------------------------------------------------


def _extract_json_object(text: str) -> str | None:
    """Best-effort extraction of a single JSON object substring.

    Strips common LLM artifacts like markdown code fences (```json...```)
    that some backends emit despite response_format=json_object instructions.
    """
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


# ---------------------------------------------------------------------------
# PdfVlService singleton
# ---------------------------------------------------------------------------


class PdfVlService:
    """Stage 6 VL service — singleton held on app.state.pdf_vl_service.

    Holds:
      - An asyncpg pool reference for Silver-tier cache reads and writes.
      - A reference to PdfRenderService for rendering page images at 200 DPI.
      - An optional pooled httpx.AsyncClient (app.state.openai_http_client).
        Falls back to a per-call httpx.AsyncClient when not present (test paths).

    All operations are async-native (I/O-bound httpx calls).  No process pool
    is needed because the heavy work (VL inference) happens out-of-process in
    Ollama / vLLM.

    Usage in FastAPI lifespan::

        app.state.pdf_vl_service = PdfVlService(
            pool=app.state.pg_pool,
            render_service=app.state.pdf_render_service,
            http_client=getattr(app.state, "openai_http_client", None),
        )

    Then in route handlers::

        svc = request.app.state.pdf_vl_service
        result, cache_hit = await svc.summarize_section(
            pdf_bytes=pdf_bytes,
            pdf_id=pdf_id,
            section_ref={"kind": "page", "page": 3},
        )
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        render_service: PdfRenderService,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._pool = pool
        self._render_service = render_service
        self._http_client = http_client  # may be None in test paths

        # Read config from environment (same pattern as pdf_ocr.py).
        self._model_id = os.environ.get("PDF_VL_MODEL_ID", _DEFAULT_MODEL_ID)
        self._backend = os.environ.get("PDF_VL_BACKEND", _DEFAULT_BACKEND)
        self._backend_url = os.environ.get("PDF_VL_BACKEND_URL", _DEFAULT_BACKEND_URL).rstrip("/")
        self._timeout_s = float(os.environ.get("PDF_VL_TIMEOUT_S", str(_DEFAULT_TIMEOUT_S)))
        self._max_pages = int(os.environ.get("PDF_VL_MAX_PAGES", str(_DEFAULT_MAX_PAGES)))

        logger.info(
            "PdfVlService ready: model=%s backend=%s url=%s timeout=%.0fs max_pages=%d",
            self._model_id,
            self._backend,
            self._backend_url,
            self._timeout_s,
            self._max_pages,
        )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def summarize_section(
        self,
        pdf_bytes: bytes,
        pdf_id: str,
        section_ref: dict[str, Any],
        workspace_id: uuid.UUID,
    ) -> tuple[dict[str, Any], bool]:
        """Summarise a section of a PDF using Qwen-VL.

        Parameters
        ----------
        pdf_bytes:
            Raw bytes of the normalised PDF (from Bronze store).
        pdf_id:
            SHA-256 hex of the PDF (cache discriminator, part of provenance).
        section_ref:
            Opaque section reference dict.  Supported shapes:
              - {"kind": "page", "page": int}
              - {"kind": "page_range", "page_start": int, "page_end": int}
              - {"kind": "layout_region", "region_id": "<UUID string>"}
        workspace_id:
            Tenant workspace UUID. Required — silver.pdf_vl_summaries.workspace_id
            is NOT NULL, and the cache is scoped per-workspace.

        Returns
        -------
        (result_dict, cache_hit)
            result_dict keys:
              summary_id, pdf_id, section_ref, summary_text, claims,
              model_id, model_backend, mean_claim_confidence,
              prompt_tokens, completion_tokens
            cache_hit: True if the result was served from silver.pdf_vl_summaries.

        Raises
        ------
        VlSectionTooLargeError
            When the resolved page list exceeds PDF_VL_MAX_PAGES.
        VlBackendError
            When the LLM backend returns non-200 or is unreachable.
        VlOutputShapeError
            When the LLM output fails VlSummaryShape Pydantic validation.
        """
        section_ref_hash = _hash_section_ref(section_ref)

        # Step 1 — cache check.
        cached = await self._cache_hit(pdf_id, section_ref_hash, workspace_id)
        if cached is not None:
            logger.debug(
                "summarize_section cache HIT pdf_id=%s section_ref=%r",
                pdf_id[:16], section_ref,
            )
            return cached, True

        logger.debug(
            "summarize_section cache MISS pdf_id=%s section_ref=%r — calling VL backend",
            pdf_id[:16], section_ref,
        )

        # Step 2 — resolve section_ref → list of 1-indexed page numbers.
        pages = await self._resolve_pages(section_ref)
        if len(pages) > self._max_pages:
            raise VlSectionTooLargeError(page_count=len(pages), max_pages=self._max_pages)

        # Step 3 — render each page at 200 DPI (VL input resolution per §04p Stage 2).
        png_list: list[bytes] = []
        for page_num in pages:
            png_bytes = await self._render_service.render_page(
                pdf_bytes=pdf_bytes,
                pdf_id=pdf_id,
                page=page_num,
                dpi=200,
            )
            png_list.append(png_bytes)

        # Step 4 — call VL backend.
        raw_content, usage = await self._call_vl_backend(png_list, pages)

        # Step 5 — validate structured output.
        vl_summary = _parse_and_validate(raw_content)

        # Step 6 — compute mean_claim_confidence.
        mean_conf: float | None = None
        if vl_summary.claims:
            mean_conf = sum(c.confidence for c in vl_summary.claims) / len(vl_summary.claims)

        # Step 7 — persist to Silver cache.
        summary_id = await self._persist(
            pdf_id=pdf_id,
            workspace_id=workspace_id,
            section_ref=section_ref,
            section_ref_hash=section_ref_hash,
            summary_text=vl_summary.summary,
            claims=[c.model_dump() for c in vl_summary.claims],
            mean_claim_confidence=mean_conf,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )

        result: dict[str, Any] = {
            "summary_id": summary_id,
            "pdf_id": pdf_id,
            "section_ref": section_ref,
            "summary_text": vl_summary.summary,
            "claims": [c.model_dump() for c in vl_summary.claims],
            "model_id": self._model_id,
            "model_backend": self._backend,
            "mean_claim_confidence": mean_conf,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        }

        logger.info(
            "summarize_section OK pdf_id=%s pages=%r claims=%d mean_conf=%s",
            pdf_id[:16], pages, len(vl_summary.claims),
            f"{mean_conf:.3f}" if mean_conf is not None else "None",
        )
        return result, False

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    async def _resolve_pages(self, section_ref: dict[str, Any]) -> list[int]:
        """Resolve a section_ref to a sorted list of 1-indexed page numbers.

        Raises ValueError on unknown section_ref kind.
        For layout_region, looks up the region's page from
        silver.pdf_layout_regions in the database.
        """
        kind = section_ref.get("kind")
        if kind == "page":
            return [int(section_ref["page"])]
        if kind == "page_range":
            start = int(section_ref["page_start"])
            end = int(section_ref["page_end"])
            return list(range(start, end + 1))
        if kind == "layout_region":
            region_id = section_ref["region_id"]
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT page FROM silver.pdf_layout_regions WHERE region_id = $1",
                    uuid.UUID(str(region_id)),
                )
            if row is None:
                raise ValueError(f"layout_region region_id={region_id!r} not found in silver.pdf_layout_regions")
            return [int(row["page"])]
        raise ValueError(f"Unknown section_ref kind: {kind!r}")

    async def _call_vl_backend(
        self,
        png_list: list[bytes],
        pages: list[int],
    ) -> tuple[str, dict[str, Any]]:
        """POST multipart chat-completions request to the VL backend.

        Returns
        -------
        (content_str, usage_dict)
            content_str: raw assistant message content (JSON string).
            usage_dict: {"prompt_tokens": int, "completion_tokens": int} from
                        the response `usage` field (may be empty if the backend
                        does not report token counts).

        Raises
        ------
        VlBackendError
            On HTTP error or non-200 status.
        """
        # Build the user message: text instruction + one image_url per page.
        user_content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"Analyse the following {len(png_list)} page image(s) from a geological PDF "
                    f"(pages {pages[0]}–{pages[-1] if len(pages) > 1 else pages[0]}) "
                    "and respond with the JSON object described in the system prompt. "
                    "Ground every claim you make to a visible location on the page."
                ),
            }
        ]
        for png_bytes in png_list:
            b64 = base64.b64encode(png_bytes).decode("ascii")
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{b64}",
                        "detail": "high",  # Ollama / OpenAI both accept this hint
                    },
                }
            )

        chat_body: dict[str, Any] = {
            "model": self._model_id,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "stream": False,
            "response_format": {"type": "json_object"},  # honoured by Ollama + vLLM for compatible models
            "temperature": 0.1,  # low — we want deterministic structured output
            "max_tokens": 2048,
        }

        endpoint = f"{self._backend_url}/chat/completions"

        try:
            if self._http_client is not None:
                resp = await self._http_client.post(
                    endpoint, json=chat_body, timeout=self._timeout_s
                )
            else:
                # Fallback for test paths or pre-pool startup.
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    resp = await client.post(endpoint, json=chat_body)
        except httpx.HTTPError as exc:
            logger.warning("VL backend unreachable: %s", exc)
            raise VlBackendError(status_code=None, detail=str(exc)) from exc

        if resp.status_code != 200:
            body_preview = resp.text[:200]
            logger.warning(
                "VL backend non-200: status=%d body=%r", resp.status_code, body_preview
            )
            raise VlBackendError(
                status_code=resp.status_code,
                detail=f"HTTP {resp.status_code}: {body_preview}",
            )

        try:
            chat_result = resp.json()
        except ValueError as exc:
            raise VlBackendError(
                status_code=resp.status_code, detail="Response body is not valid JSON"
            ) from exc

        try:
            content: str = chat_result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            logger.warning("VL backend shape drift: %r", chat_result)
            raise VlBackendError(
                status_code=resp.status_code,
                detail="Unexpected response shape (missing choices[0].message.content)",
            ) from exc

        if not isinstance(content, str) or not content.strip():
            raise VlBackendError(
                status_code=resp.status_code, detail="Empty assistant message content"
            )

        usage: dict[str, Any] = {}
        raw_usage = chat_result.get("usage", {})
        if isinstance(raw_usage, dict):
            if "prompt_tokens" in raw_usage:
                usage["prompt_tokens"] = int(raw_usage["prompt_tokens"])
            if "completion_tokens" in raw_usage:
                usage["completion_tokens"] = int(raw_usage["completion_tokens"])

        return content, usage

    async def _cache_hit(
        self, pdf_id: str, section_ref_hash: str, workspace_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        """Check silver.pdf_vl_summaries for a cached result.

        Returns the cached result dict (matching the summarize_section return
        shape) or None if no cached row exists for this
        (workspace_id, pdf_id, hash, model).
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT summary_id, section_ref, summary_text, claims,"
                "       model_id, model_backend, mean_claim_confidence,"
                "       prompt_tokens, completion_tokens"
                " FROM silver.pdf_vl_summaries"
                " WHERE workspace_id = $1 AND pdf_id = $2"
                "   AND section_ref_hash = $3 AND model_id = $4",
                workspace_id, pdf_id, section_ref_hash, self._model_id,
            )
        if row is None:
            return None

        claims_raw = row["claims"]
        if isinstance(claims_raw, str):
            claims_raw = json.loads(claims_raw)

        section_ref_raw = row["section_ref"]
        if isinstance(section_ref_raw, str):
            section_ref_raw = json.loads(section_ref_raw)

        return {
            "summary_id": row["summary_id"],
            "pdf_id": pdf_id,
            "section_ref": section_ref_raw,
            "summary_text": row["summary_text"],
            "claims": claims_raw,
            "model_id": row["model_id"],
            "model_backend": row["model_backend"],
            "mean_claim_confidence": row["mean_claim_confidence"],
            "prompt_tokens": row["prompt_tokens"],
            "completion_tokens": row["completion_tokens"],
        }

    async def _persist(
        self,
        pdf_id: str,
        workspace_id: uuid.UUID,
        section_ref: dict[str, Any],
        section_ref_hash: str,
        summary_text: str,
        claims: list[dict[str, Any]],
        mean_claim_confidence: float | None,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> uuid.UUID:
        """INSERT a new VL summary row into silver.pdf_vl_summaries.

        Returns the generated summary_id UUID.
        ON CONFLICT DO NOTHING handles any concurrent-insert race — the
        unique index on (pdf_id, section_ref_hash, model_id) prevents
        duplicate rows.
        """
        summary_id = uuid.uuid4()
        now = datetime.now(tz=UTC)

        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO silver.pdf_vl_summaries"
                " (summary_id, workspace_id, pdf_id, section_ref, section_ref_hash,"
                "  summary_text, claims, model_id, model_backend,"
                "  mean_claim_confidence, prompt_tokens, completion_tokens,"
                "  extracted_at)"
                " VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7::jsonb, $8, $9,"
                "  $10, $11, $12, $13)"
                " ON CONFLICT (pdf_id, section_ref_hash, model_id) DO NOTHING",
                summary_id,
                workspace_id,
                pdf_id,
                json.dumps(section_ref),
                section_ref_hash,
                summary_text,
                json.dumps(claims),
                self._model_id,
                self._backend,
                mean_claim_confidence,
                prompt_tokens,
                completion_tokens,
                now,
            )

        logger.debug(
            "Persisted VL summary summary_id=%s pdf_id=%s claims=%d",
            summary_id, pdf_id[:16], len(claims),
        )
        return summary_id


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _hash_section_ref(section_ref: dict[str, Any]) -> str:
    """Compute sha256 of the canonicalised section_ref JSON.

    sort_keys=True makes the hash deterministic regardless of JSONB key
    ordering in the dict (Python dicts preserve insertion order in 3.7+
    but callers may not guarantee key order when constructing section_refs).
    """
    canonical = json.dumps(section_ref, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _parse_and_validate(raw_content: str) -> VlSummaryShape:
    """Parse the LLM response string and validate it as VlSummaryShape.

    Handles markdown-fenced JSON via _extract_json_object (some backends
    emit ```json...``` despite response_format=json_object instructions).

    Raises
    ------
    VlOutputShapeError
        When the content cannot be parsed as JSON or fails Pydantic validation.
    """
    try:
        parsed = json.loads(raw_content)
    except ValueError:
        # Attempt markdown-fence recovery.
        stripped = _extract_json_object(raw_content)
        if stripped is None:
            raise VlOutputShapeError(
                f"LLM response is not JSON and contains no extractable JSON object. "
                f"First 200 chars: {raw_content[:200]!r}"
            )
        try:
            parsed = json.loads(stripped)
        except ValueError as exc:
            raise VlOutputShapeError(
                f"Extracted JSON substring still fails to parse: {exc}"
            ) from exc

    if not isinstance(parsed, dict):
        raise VlOutputShapeError(
            f"LLM response parsed to {type(parsed).__name__}, expected dict"
        )

    try:
        return VlSummaryShape.model_validate(parsed)
    except Exception as exc:
        raise VlOutputShapeError(
            f"Pydantic validation failed: {exc}"
        ) from exc
