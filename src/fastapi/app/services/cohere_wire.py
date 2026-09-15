"""The Cohere API wire contract, as data (ADR-0023).

The sibling of ``bedrock_wire.py``, for the two capabilities that left
Bedrock on 2026-09-15: chat (Command A+) and OCR (Parse 5). Both are AWS
Marketplace SageMaker packages rather than Bedrock models, on instance
classes that bill while idle, so they moved to Cohere's own API.

Everything ``bedrock_wire``'s docstring says about WHY a contract-as-data is
worth having applies here unchanged, and is not repeated: read that module
first. The machinery — ``Field``, ``Status``, ``WireContract``,
``diff_section`` — is imported from it rather than duplicated, because two
copies of a diffing rule is how the two contracts start disagreeing about
what "confirmed" means.

One status value does NOT appear here, and its absence is the point.
``CARRIED`` means "observed on Azure AI Foundry by a live call on
2026-07-30, assumed to survive the host change". Chat has three of those on
Bedrock. Parse has none, on any host — it has never been empirically
verified anywhere, which is why its response side is almost entirely
``TOLERATED`` alternates. The adapter is guessing in three places at once
and degrading rather than crashing is the whole design.

For chat, the three Foundry observations are recorded on ``CHAT_CONVERSE``
in ``bedrock_wire`` and they do not carry a second time: a different host is
a second chance to be wrong in the same way. ``llm_cohere``'s own docstring
lists what it assumes; a declared contract for it is still to be written,
and this module is where it goes.
"""

from __future__ import annotations

from app.services.bedrock_wire import Field, Status, WireContract

__all__ = ["CONTRACTS", "PARSE"]


# ---------------------------------------------------------------------------
# Parse — POST {COHERE_BASE_URL}/v2/parse
# ---------------------------------------------------------------------------
# NEVER EMPIRICALLY VERIFIED, on Foundry, on Bedrock, or here. That is a
# stronger statement than anything in bedrock_wire: for the other calls a
# previous host at least confirmed a related shape. Here nothing ever has.
#
# The request half is the only part that changed in the move. ``model`` is
# back in the body — Bedrock had lifted it out to ``modelId`` — which is the
# shape ADR-0019 first wrote against when Foundry proxied Cohere's own API
# at ``{endpoint}/providers/cohere/v2/parse``. The RESPONSE half below is
# byte-for-byte what it was on Bedrock, because it describes what the MODEL
# returns and the response adapter has not been touched on any of the three
# moves.

PARSE = WireContract(
    name="parse",
    service="cohere-api",
    method="POST /v2/parse",
    probe_section="parse",
    request=(
        Field(
            "model",
            Status.ASSUMED,
            True,
            "COHERE_PARSE_MODEL — 'parse-v5.0' by default. A plain model name; there is no endpoint indirection on this host.",
        ),
        Field("document.type", Status.ASSUMED, True, "Literal 'image_url'."),
        Field(
            "document.image_url.url",
            Status.ASSUMED,
            True,
            "One page as a data: URI. Oversized plan sheets are DOWNSCALED, "
            "not tiled — Parse returns no word polygons to stitch tiles with.",
        ),
        Field("output_format", Status.ASSUMED, True, "'blocks' or 'markdown'."),
    ),
    response=(
        Field("pages[]", Status.ASSUMED, True, "One entry; only pages[0] is read."),
        Field("blocks[]", Status.TOLERATED, False, "Top-level, if the response omits the pages wrapper."),
        Field("markdown", Status.TOLERATED, False, "Same, for markdown mode."),
        Field("pages[].blocks[].type", Status.ASSUMED, False, "'text' | 'table' | 'image'; defaults to text."),
        Field("pages[].blocks[].text", Status.ASSUMED, False, "Text block content — first spelling tried."),
        Field("pages[].blocks[].content", Status.TOLERATED, False, "Second spelling."),
        Field("pages[].blocks[].markdown", Status.TOLERATED, False, "Third spelling."),
        Field("pages[].blocks[].html", Status.ASSUMED, False, "Table block, as an HTML fragment."),
        Field("pages[].blocks[].description", Status.ASSUMED, False, "Image block description."),
        Field("pages[].blocks[].caption", Status.TOLERATED, False, "Alternate spelling of the same."),
        Field("pages[].markdown", Status.ASSUMED, False, "Markdown mode, as a bare string."),
        Field("pages[].markdown.content", Status.TOLERATED, False, "Markdown mode, as an object."),
    ),
    notes=(
        "Parse returns no per-word confidence and no polygons, so "
        "PageOcrResult carries confidence_reported=False and the persist path "
        "stores ocr_confidence as NULL. That is a property of the model, not "
        "of the wire, and it does not change with the host.",
        "Authentication is a bearer key, not SigV4. The practical consequence "
        "is that a refused call produces NO AWS metric — CloudWatch cannot "
        "see a request that never went to AWS — so the COHERE_PARSE_REJECTED "
        "log line is the entire signal, where on Bedrock it was a backstop to "
        "InvocationClientErrors.",
    ),
)


CONTRACTS: tuple[WireContract, ...] = (PARSE,)
