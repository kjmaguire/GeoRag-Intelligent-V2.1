"""Page-image verbalization via a Foundry vision model (2026-08-18).

What this is for
----------------
Maps, cross-sections and plan views carry their meaning in the picture. Embed
v4 makes such a page *findable* (see page_image.py), but the chat model
(Cohere Command A+) is text-only, so a retrieved page image has nothing the
answer path can quote, cite or ground against. Verbalization closes that: a
vision model describes the page, the description becomes the passage text, and
every downstream layer — reranker, citations, Section 04i numeric grounding —
then works on it exactly as it does on any other passage.

Why not Cohere
--------------
Command A Vision is not in the Azure Foundry catalog. Checked directly against
the catalog 2026-08-18 (150 models): Cohere offers exactly six there —
command-a, command-a-plus, embed-v3-multilingual, embed-v-4-0, and two
rerankers — and none of them accept an image.

Going direct to Cohere's own API would have meant a second vendor, a second
credential, and page images of tenant geology egressing out of Azure. The
verbalization job doesn't care which model does it, so it uses a Foundry
vision model instead: same endpoint, same key, same network boundary as every
other model in the stack. Kyle's call 2026-08-18: `gpt-5-mini`.

Wire contract
-------------
Reuses the unified OpenAI v1 surface this repo already confirmed against a
live Foundry deployment 2026-07-30 (see config.py AZURE_FOUNDRY_*):

    POST {endpoint}/openai/v1/chat/completions

with OpenAI-style multimodal content parts. This is the same path
`_call_openai_compatible_llm` uses for chat, so endpoint/auth behaviour is
already proven — only the image content part is new.

Fail-soft contract
------------------
Verbalization is additive: without it, an image passage keeps its placeholder
text and is still retrievable by image vector. So every failure path returns
`ok=False` rather than raising, and the sweep leaves `verbalized_at` NULL so
the page is retried next pass. A vision outage must never cost the ingest
pipeline anything.

Hallucination posture
---------------------
A VLM returns fluent text with no per-token confidence — the opposite of
Document Intelligence, which this does NOT replace and must never be used to
replace. PROMPT therefore asks for a DESCRIPTION of what the page depicts and
explicitly forbids transcribing numeric values out of tables. If a number
matters, it must come from the OCR path that can attach a confidence to it.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("georag.ingest.page_vision")

ENABLED_ENV = "IMAGE_VERBALIZATION_ENABLED"
MODEL_ENV = "IMAGE_VERBALIZATION_MODEL"

# Reuses the LLM's Foundry credentials deliberately — one endpoint, one key.
ENDPOINT_ENV = "AZURE_FOUNDRY_ENDPOINT"
KEY_ENV = "AZURE_FOUNDRY_API_KEY"

_DEFAULT_MODEL = "gpt-5-mini"

# A page description is a paragraph, not an essay. Capping output keeps the
# passage inside the reranker's window and stops the model padding a sparse
# figure into prose that reads more informative than the page is.
_MAX_TOKENS = int(os.environ.get("IMAGE_VERBALIZATION_MAX_TOKENS", "400"))

# Bounded like the DI client's caps, and for the same reason: a hung request
# inside a sweep starves the worker.
_TIMEOUT_S = float(os.environ.get("IMAGE_VERBALIZATION_TIMEOUT_S", "120"))

# The instruction is deliberately restrictive. A vision model asked to "read
# this page" will happily transcribe an assay table — fluently, plausibly, and
# with the occasional invented digit that no confidence score flags. Against
# NI 43-101 grade tables that is the single worst failure this system can
# have, and it would walk straight through the Section 04i layers because the
# output looks like properly extracted text.
#
# So: describe, don't transcribe. Numbers come from the OCR path, which can
# attach a confidence to each one. What we want from the picture is the thing
# OCR cannot give us — what it IS, what it SHOWS, and the named entities on it
# that make it findable.
#: What the model is asked for at each detail level.
#:
#: These MUST move together. Until 2026-08-22 the prompt asked for the
#: figure's "title and caption, quoted exactly" and for drill-hole IDs,
#: grid and scale -- while the image was sent at detail="low", a single
#: downsampled tile. A model asked to quote exactly from an image it
#: cannot read does not refuse; it writes plausible text. Correctly
#: formatted hole IDs that appear on no sheet then become
#: silver.document_passages.text and reach the answer path.
#:
#: Derived rather than written side by side, for the same reason the DI
#: timeout pair is derived: raising IMAGE_VERBALIZATION_DETAIL now
#: changes what is asked for, so the two cannot disagree again.
_HIGH_DETAIL_ASKS = (
    "- Its title and caption, quoted exactly.\n"
    "- Named entities: property, deposit, zone, formation, fault, drill-hole "
    "IDs, grid or coordinate system, scale, orientation.\n"
)

#: At low detail the page is one downsampled tile. Labels, hole IDs and
#: scale bars are below the resolution the model receives, so asking for
#: them invites invention. Subject matter survives downsampling; text on
#: the sheet does not.
_LOW_DETAIL_ASKS = (
    "- The property, deposit or area it covers, ONLY if it is legible. If "
    "any label, title or identifier is not clearly readable, say so rather "
    "than guessing it.\n"
)


def image_detail() -> str:
    """The detail level the vision request is sent at."""
    value = (os.environ.get("IMAGE_VERBALIZATION_DETAIL", "low") or "low").strip()
    return value.lower() or "low"


def build_prompt(detail: str | None = None) -> str:
    """The verbalization prompt, matched to the resolution being sent."""
    level = (detail or image_detail()).lower()
    asks = _HIGH_DETAIL_ASKS if level == "high" else _LOW_DETAIL_ASKS

    return (
    "You are describing one page of a geological or mining technical report so "
    "that it can be found by search. Describe what this page depicts.\n\n"
    "Include, when present:\n"
    "- The kind of figure it is (geological map, cross-section, plan view, "
    "long section, drill-hole trace, stratigraphic column, photograph, chart).\n"
    + asks +
    "- What the figure shows in geological terms: units, structures, "
    "mineralisation, alteration, spatial relationships.\n\n"
    "Rules:\n"
    "- Do NOT transcribe numeric values out of data tables, and do not list "
    "assay results, grades or tonnages. Say that a table is present and what "
    "it concerns.\n"
    "- Do not infer or estimate anything the page does not state.\n"
    "- Never write an identifier, title or caption you cannot actually read "
    "on the page. A description that omits a label is useful; one that "
    "invents a plausible label is worse than nothing.\n"
    "- If the page is blank, a cover sheet, or plain body text with no figure, "
    "say exactly that in one short sentence.\n"
    "- Write plain prose. No preamble, no markdown headings."
    )


#: Back-compat alias. Several docstrings in this module and in
#: page_verbalizer refer to "PROMPT"; it is the low-detail form, which is
#: what the live worker sends.
PROMPT = build_prompt("low")


@dataclass(frozen=True, slots=True)
class VerbalizationResult:
    """Same fail-soft shape as document_intelligence_client.PageOcrResult."""

    text: str
    ok: bool = True
    error: str | None = None


def is_enabled() -> bool:
    """Strict opt-in — unset behaves as off.

    Mirrors document_intelligence_client.is_engine_selected: importing this
    module is always safe, and no live behaviour changes until an operator
    flips the flag.
    """
    return (os.environ.get(ENABLED_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def is_configured() -> bool:
    return bool((os.environ.get(ENDPOINT_ENV) or "").strip()) and bool(
        (os.environ.get(KEY_ENV) or "").strip()
    )


def _model() -> str:
    return (os.environ.get(MODEL_ENV) or _DEFAULT_MODEL).strip()


def verbalize_page(png_bytes: bytes, *, mime: str = "image/png") -> VerbalizationResult:
    """Describe one page image. Never raises — see the fail-soft note above."""
    if not is_enabled():
        return VerbalizationResult("", ok=False, error="disabled")

    endpoint = (os.environ.get(ENDPOINT_ENV) or "").strip()
    api_key = (os.environ.get(KEY_ENV) or "").strip()
    if not (endpoint and api_key):
        return VerbalizationResult(
            "", ok=False,
            error=f"{ENABLED_ENV} is on but {ENDPOINT_ENV}/{KEY_ENV} are not set",
        )

    import httpx  # noqa: PLC0415

    url = f"{endpoint.rstrip('/')}/openai/v1/chat/completions"
    data_uri = f"data:{mime};base64,{base64.b64encode(png_bytes).decode('ascii')}"

    # Resolved once, then used for BOTH the prompt and the image detail.
    # Reading the environment twice would let the two halves of a request
    # describe different resolutions.
    _detail = image_detail()

    try:
        resp = httpx.post(
            url,
            headers={"api-key": api_key, "Content-Type": "application/json"},
            timeout=_TIMEOUT_S,
            json={
                "model": _model(),
                "max_completion_tokens": _MAX_TOKENS,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": build_prompt(_detail)},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": data_uri,
                                    # "low" is deliberate: we want the gist of
                                    # a figure, not a pixel-accurate read, and
                                    # detail="high" tiles the image into many
                                    # more tokens per page. At scope=all that
                                    # multiplies across every page of every
                                    # document.
                                    #
                                    # The PROMPT is derived from this value
                                    # (see build_prompt). It used to ask for
                                    # captions "quoted exactly" and for
                                    # drill-hole IDs regardless, which at low
                                    # detail asks the model to read text it
                                    # was not sent.
                                    "detail": _detail,
                                },
                            },
                        ],
                    }
                ],
            },
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — fail-soft by contract
        logger.warning("page_vision: request failed: %s", exc)
        return VerbalizationResult("", ok=False, error=f"{type(exc).__name__}: {exc}")

    try:
        text = _extract_text(resp.json())
    except Exception as exc:  # noqa: BLE001
        logger.warning("page_vision: could not parse response: %s", exc)
        return VerbalizationResult("", ok=False, error=f"unparseable_response: {exc}")

    if not text.strip():
        # A refusal or a length-capped empty completion. Better to retry next
        # sweep than to overwrite the placeholder with nothing.
        return VerbalizationResult("", ok=False, error="empty_description")

    return VerbalizationResult(text.strip())


def _extract_text(payload: dict) -> str:
    """Pull the assistant text out of an OpenAI-shaped chat completion.

    Tolerant of `content` arriving as a bare string (the usual shape) or as a
    list of typed parts, which some Foundry-hosted models emit.
    """
    choices = payload.get("choices") or []
    if not choices:
        raise ValueError("response contained no choices")

    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text")
        )
    raise ValueError(f"unexpected content type: {type(content).__name__}")
