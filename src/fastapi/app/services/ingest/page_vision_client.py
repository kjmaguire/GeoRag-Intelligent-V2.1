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

Going direct to a second vendor's API would have meant a second credential
and page images of tenant geology egressing outside the cloud boundary. The
verbalization job doesn't care which model does it, so it uses whatever
vision model the platform's own model service offers: same credentials, same
network boundary as every other model in the stack. Kyle's call 2026-08-18
was Foundry's `gpt-5-mini`.

⚠️ NO DIRECT EQUIVALENT ON AWS (ADR-0022). `gpt-5-mini` was an Azure OpenAI
model on the Foundry resource; Bedrock does not serve it, and unlike chat,
embeddings, reranking and OCR this capability was never a Cohere model, so
"keep the model, change the host" does not apply. A Bedrock vision model has
to be CHOSEN — it is a model decision, not a migration mechanic, and picking
one silently would change what every image passage says with no eval behind
it.

This module therefore reports itself unconfigured on AWS until
`BEDROCK_VISION_MODEL_ID` is set. That is not a regression in practice:
`IMAGE_VERBALIZATION_ENABLED` is unset in every environment, the hourly
`verbalize_page_images` cron returns before touching Postgres, and the
feature has never run in production.

Wire contract
-------------
Bedrock Converse, which is multimodal and takes image bytes directly rather
than as a data URI::

    bedrock-runtime.converse(
        modelId=<BEDROCK_VISION_MODEL_ID>,
        messages=[{"role": "user", "content": [
            {"text": <prompt>},
            {"image": {"format": "png", "source": {"bytes": <raw bytes>}}}]}],
        inferenceConfig={"maxTokens": ...})

[UNVERIFIED] — no live call has confirmed this, and it cannot be confirmed
until a model is chosen.

Fail-soft contract
------------------
Verbalization is additive: without it, an image passage keeps its placeholder
text and is still retrievable by image vector. So every failure path returns
`ok=False` rather than raising, and the sweep leaves `verbalized_at` NULL so
the page is retried next pass. A vision outage must never cost the ingest
pipeline anything.

Hallucination posture
---------------------
A VLM returns fluent text with no per-token confidence. So does the OCR
engine since ADR-0019 — Cohere Parse is itself a vision-language model —
but the two jobs stay separate: Parse TRANSCRIBES a page and its output is
scored by the quality router on content signals and routed to review;
this client DESCRIBES a page for image retrieval. PROMPT therefore asks
for a DESCRIPTION of what the page depicts and explicitly forbids
transcribing numeric values out of tables. If a number matters, it must
come from the OCR path, where it is a citable passage with provenance.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("georag.ingest.page_vision")

ENABLED_ENV = "IMAGE_VERBALIZATION_ENABLED"
MODEL_ENV = "IMAGE_VERBALIZATION_MODEL"

# Bedrock model id for the vision model. NO DEFAULT, deliberately: there is
# no Bedrock equivalent of the retired `gpt-5-mini` and guessing one would
# silently change what every image passage says. See the module docstring.
MODEL_ID_ENV = "BEDROCK_VISION_MODEL_ID"

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
    """How cautious the prompt should be about reading text off the page.

    Named for the Foundry-era `detail` request knob it used to set, which
    Bedrock Converse has no equivalent of. Since 2026-09-08 it selects the
    prompt only — the model always receives the full image — so this is now
    a statement about how much the prompt may ask for, not about what the
    model is sent. See verbalize_page.
    """
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
    """Same fail-soft shape as ocr_types.PageOcrResult."""

    text: str
    ok: bool = True
    error: str | None = None


def is_enabled() -> bool:
    """Strict opt-in — unset behaves as off.

    Mirrors cohere_parse_client.is_engine_selected: importing this
    module is always safe, and no live behaviour changes until an operator
    flips the flag.
    """
    return (os.environ.get(ENABLED_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def is_configured() -> bool:
    return bool(_model())


def _model() -> str:
    """The Bedrock model id, or "" when none has been chosen.

    MODEL_ENV (IMAGE_VERBALIZATION_MODEL) is still read first so an existing
    deployment's override keeps working, but it now carries a Bedrock model
    id rather than a Foundry deployment name.
    """
    return (
        (os.environ.get(MODEL_ENV) or os.environ.get(MODEL_ID_ENV) or "").strip()
    )


def verbalize_page(png_bytes: bytes, *, mime: str = "image/png") -> VerbalizationResult:
    """Describe one page image. Never raises — see the fail-soft note above."""
    if not is_enabled():
        return VerbalizationResult("", ok=False, error="disabled")

    model_id = _model()
    if not model_id:
        return VerbalizationResult(
            "", ok=False,
            error=(
                f"{ENABLED_ENV} is on but no vision model is configured. Set "
                f"{MODEL_ID_ENV} to a Bedrock model id — there is no default, "
                "because Bedrock has no equivalent of the retired gpt-5-mini "
                "and guessing one would change every image description "
                "(ADR-0022)."
            ),
        )

    # Resolved once, then used for BOTH the prompt and the image detail.
    # Reading the environment twice would let the two halves of a request
    # describe different resolutions.
    _detail = image_detail()

    # Converse takes raw bytes, not a data URI — one less base64 round trip
    # than the OpenAI-shaped path, and no `detail` knob.
    #
    # ⚠️ That knob did two jobs and only one survives. `detail: low` bounded
    # per-page token cost (at IMAGE_EMBED_PAGE_SCOPE=all, across every page
    # of every document) AND told build_prompt how cautious to be about
    # asking the model to read text. Bedrock has no equivalent, so
    # image_detail() now selects the PROMPT ONLY: the model always sees the
    # full image. That makes "low" the conservative prompt rather than a
    # cheaper request, and it means cost control here is _MAX_TOKENS and the
    # prompt alone. Watch the per-page cost if this is ever switched on.
    _format = "png" if mime.endswith("png") else mime.rsplit("/", 1)[-1]

    try:
        from app.services._bedrock import get_client  # noqa: PLC0415

        resp = get_client("bedrock-runtime", read_timeout_s=_TIMEOUT_S).converse(
            modelId=model_id,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"text": build_prompt(_detail)},
                        {"image": {"format": _format, "source": {"bytes": png_bytes}}},
                    ],
                }
            ],
            inferenceConfig={"maxTokens": _MAX_TOKENS},
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft by contract
        logger.warning("page_vision: request failed: %s", exc)
        return VerbalizationResult("", ok=False, error=f"{type(exc).__name__}: {exc}")

    try:
        text = _extract_text(resp)
    except Exception as exc:  # noqa: BLE001
        logger.warning("page_vision: could not parse response: %s", exc)
        return VerbalizationResult("", ok=False, error=f"unparseable_response: {exc}")

    if not text.strip():
        # A refusal or a length-capped empty completion. Better to retry next
        # sweep than to overwrite the placeholder with nothing.
        return VerbalizationResult("", ok=False, error="empty_description")

    return VerbalizationResult(text.strip())


def _extract_text(payload: dict) -> str:
    """Pull the assistant text out of a Bedrock Converse response.

    Converse always returns content as a list of typed blocks, so unlike the
    OpenAI-shaped predecessor there is no bare-string case to tolerate. Any
    non-text block (a reasoning trace, say) is skipped rather than
    stringified into the page description.
    """
    message = (payload.get("output") or {}).get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        raise ValueError(f"unexpected content type: {type(content).__name__}")
    return "\n".join(
        block["text"] for block in content if isinstance(block, dict) and "text" in block
    )
