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
a second chance to be wrong in the same way. ``CHAT_V2`` below re-asks all
three.
"""

from __future__ import annotations

from typing import Any

from app.services.bedrock_wire import Field, Status, WireContract, diff_section

__all__ = ["CHAT_V2", "CONTRACTS", "PARSE", "diff_report"]


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
            "document.image_url",
            Status.ASSUMED,
            True,
            "One page as a data: URI, as a bare STRING. Until 2026-09-23 this "
            "was declared as `document.image_url.url` — chat's image object — "
            'and the first live call was refused with HTTP 400 "parameter '
            "'document.image_url' is of type object but should be of type "
            'string". That rejection is the only live evidence: ASSUMED, '
            "not observed, until a string-form call succeeds. Oversized plan "
            "sheets are DOWNSCALED, not tiled — "
            "Parse returns no word polygons to stitch tiles with.",
        ),
        Field("output_format", Status.ASSUMED, True, "'blocks' or 'markdown'."),
    ),
    # The probe records `pages[0]`'s key set per output format, which is
    # exactly the list this diff needs. The Bedrock version of this contract
    # declared the same response fields with NO evidence path at all, so its
    # parse diff could only ever say "not_observed" — twelve declared fields
    # that no run could confirm or contradict. Fixed here rather than
    # carried, because a contract nothing can check is the thing this file's
    # sibling docstring warns about.
    evidence_paths=("formats.*.page0_keys", "formats.*.block_keys", "formats.*.block_payload_keys"),
    response=(
        Field("pages[]", Status.ASSUMED, True, "One entry; only pages[0] is read."),
        Field(
            "pages[].index",
            Status.ASSUMED,
            False,
            "Zero-based page index, per the Cohere SDK's ParsePage. Nothing reads it: one request is one page.",
            evidence_key="index",
        ),
        Field(
            "pages[].blocks[].table",
            Status.ASSUMED,
            False,
            "The Cohere SDK (7.1.1, types/parse_block.py) nests each block's "
            'fields under a key named by its type: {"type": "text", '
            '"text": {"content": ...}}. _block_fields reads there first '
            "and falls back to the flat spelling below. This field is the "
            "table payload ({html, title, description, bounding boxes}).",
            evidence_key="table",
        ),
        Field(
            "pages[].blocks[].image",
            Status.ASSUMED,
            False,
            "Image block payload (description, category, bounding boxes).",
            evidence_key="image",
        ),
        Field(
            "pages[].blocks[].*.bounding_box",
            Status.TOLERATED,
            False,
            "Block geometry in the SDK shape. Nothing reads it; declared so "
            "an observed field is not a fresh discovery on every run.",
            evidence_key="bounding_box",
        ),
        Field(
            "pages[].blocks[].*.bounding_box_normalized",
            Status.TOLERATED,
            False,
            "The same geometry in 0..1 page units. Nothing reads it.",
            evidence_key="bounding_box_normalized",
        ),
        Field(
            "blocks[]",
            Status.TOLERATED,
            False,
            "Top-level, if the response omits the pages wrapper.",
            evidence_key="blocks",
        ),
        Field(
            "markdown",
            Status.TOLERATED,
            False,
            "Same, for markdown mode.",
            evidence_key="markdown",
        ),
        Field(
            "pages[].blocks[].type",
            Status.ASSUMED,
            False,
            "'text' | 'table' | 'image'; defaults to text.",
            evidence_key="type",
        ),
        Field(
            "pages[].blocks[].text",
            Status.ASSUMED,
            False,
            "In the SDK shape, the text block's payload OBJECT; in the flat "
            "one, the text itself. _first_str takes only a string, so the "
            "object's repr can never become page text again.",
            evidence_key="text",
        ),
        Field(
            "pages[].blocks[].text.content",
            Status.ASSUMED,
            False,
            "The text, in the SDK shape — first spelling tried.",
            evidence_key="content",
        ),
        Field(
            "pages[].blocks[].markdown",
            Status.TOLERATED,
            False,
            "Third spelling.",
            evidence_key="markdown",
        ),
        Field(
            "pages[].blocks[].html",
            Status.ASSUMED,
            False,
            "Table block, as an HTML fragment.",
            evidence_key="html",
        ),
        Field(
            "pages[].blocks[].description",
            Status.ASSUMED,
            False,
            "Image block description.",
            evidence_key="description",
        ),
        Field(
            "pages[].blocks[].caption",
            Status.TOLERATED,
            False,
            "Alternate spelling of the same.",
            evidence_key="caption",
        ),
        Field(
            "pages[].markdown",
            Status.ASSUMED,
            False,
            "Markdown mode, as a bare string.",
            evidence_key="markdown",
        ),
        Field(
            "pages[].markdown.content",
            Status.TOLERATED,
            False,
            "Markdown mode, as an object. No evidence_key: the probe records "
            "page0's keys, not the keys inside a markdown object, and a "
            "declared key the probe can never see reports as contradicted "
            "forever — which teaches the reader to ignore the column.",
        ),
        Field(
            "pages[].bbox",
            Status.TOLERATED,
            False,
            "Page-level geometry, if present. Nothing reads it; declared so "
            "an observed field is not reported as a fresh discovery on "
            "every run.",
            evidence_key="bbox",
        ),
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


# ---------------------------------------------------------------------------
# Chat — POST {COHERE_BASE_URL}/v2/chat
# ---------------------------------------------------------------------------
# Three of these re-ask questions that documentation got WRONG once already.
# On Azure AI Foundry, a single live call on 2026-07-30 settled all three
# against what the docs said — the Foundry catalog table claimed "Text only"
# response formats for Command A+ while Cohere's own docs listed it under
# Structured Outputs. Those observations are recorded on `CHAT_CONVERSE` in
# bedrock_wire as CARRIED, and they do not carry a second time. A different
# host is a second chance to be wrong in the same way, and this is the third
# host.
#
#   (1) `response_format: {"type": "json_object"}` — the single most
#       important field in this module. Every typed-output guard in
#       orchestrator_validators.py assumes the model was actually asked for
#       JSON (CLAUDE.md hard rule 4). If it is silently ignored, the guards
#       start rejecting prose the model was never told not to write, and the
#       symptom is a refusal rate rather than an error.
#   (2) Where reasoning lands. Foundry used a sibling `reasoning_content`
#       field; Bedrock Converse uses a `reasoningContent` content block.
#       Cohere's own v2 could do either, or neither.
#   (3) Whether the `<|START_TEXT|>`/`<|END_TEXT|>` sentinels survive.
#       `clean_model_text` strips them unconditionally, so the adapter does
#       not care which way this goes — but the probe records it, because a
#       host that strips them means the stripping is dead code on this path
#       and a future reader should know that rather than guess.

CHAT_V2 = WireContract(
    name="chat_v2",
    service="cohere-api",
    method="POST /v2/chat",
    probe_section="chat",
    evidence_paths=("*.content_block_keys", "*.message_sibling_keys"),
    request=(
        Field("model", Status.ASSUMED, True, "COHERE_CHAT_MODEL — 'command-a-plus-05-2026'."),
        Field(
            "messages[].role",
            Status.ASSUMED,
            True,
            "System is a MESSAGE here, not a top-level parameter. That is the "
            "opposite of Bedrock Converse and it fails SILENTLY when reversed: "
            "a system prompt sent as a user turn still returns fluent text, "
            "just without the grounding rules applied.",
        ),
        Field("messages[].content", Status.ASSUMED, True, "Plain string per message."),
        Field("temperature", Status.ASSUMED, True, "Caller's value, unmodified."),
        Field(
            "max_tokens",
            Status.ASSUMED,
            True,
            "Capped by llm_common.cap_output_tokens so prompt + output cannot "
            "overflow COHERE_CHAT_MAX_MODEL_LEN. Providers answer 400 rather "
            "than truncating, so an uncapped request fails AFTER paying to "
            "build the prompt.",
        ),
        Field("stream", Status.ASSUMED, True, "True exactly when a token_callback was supplied."),
        Field(
            "response_format.type",
            Status.ASSUMED,
            False,
            "'json_object', sent only when the caller asks for JSON. Unlike "
            "Converse, which has no first-class JSON mode and rides a "
            "passthrough field, this is a documented top-level parameter — "
            "which is a reason to expect it to work and not a reason to "
            "assume it does. Hard rule 4 depends on it being honoured.",
        ),
    ),
    response=(
        Field(
            "message.content[].type",
            Status.ASSUMED,
            False,
            "Block discriminator, 'text' for the answer. Declared because a "
            "live-shaped run reported it as an UNDECLARED field — which is "
            "the discovery half of this contract working, on the first "
            "exercise of it.",
            evidence_key="type",
        ),
        Field(
            "message.content[].text",
            Status.ASSUMED,
            True,
            "The answer, concatenated across typed blocks.",
            evidence_key="text",
        ),
        Field(
            "message.content (bare string)",
            Status.TOLERATED,
            False,
            "Plausible neighbour of the typed-block shape; _extract_content "
            "accepts it rather than betting on one spelling.",
        ),
        Field(
            "text",
            Status.TOLERATED,
            False,
            "Pre-v2 top-level spelling. Worth accepting rather than failing a live call over a field name.",
        ),
        Field(
            "message.content[].thinking",
            Status.ASSUMED,
            False,
            "Where reasoning lands on this host — question (2) above. The "
            "2026-09-23 run from inside the VPC saw content blocks keyed "
            "{type, text, thinking} and no `reasoning_content` sibling (which "
            "this field used to declare, and which that run reported as "
            "contradicted). Reasoning is on by default and counts against "
            "max_tokens; a reply that is all thinking is a budget outcome "
            '(_extract_content returns ""), not an unreadable shape.',
            evidence_key="thinking",
        ),
        Field(
            "message.role",
            Status.ASSUMED,
            False,
            "'assistant'. Nothing reads it; declared because the same run reported it as undeclared.",
            evidence_key="role",
        ),
        Field("usage.tokens.input_tokens", Status.ASSUMED, False, "v2 nests real counts under `tokens`."),
        Field("usage.tokens.output_tokens", Status.ASSUMED, False, "Same."),
        Field(
            "usage.billed_units",
            Status.TOLERATED,
            False,
            "The billing view, which can differ from the token counts. "
            "_extract_usage prefers `tokens` and falls back to the flat shape.",
        ),
    ),
    notes=(
        "Streaming is SSE with type-tagged events: text on `content-delta`, "
        "usage on `message-end`. _delta_text is tolerant across the nested "
        "and flat spellings because a missed delta is a silently truncated "
        "answer, not an error.",
        "The 2026-09-23 live run parsed ZERO `data:` frames from a 200 "
        "streaming response, while sending `Accept: application/json`; "
        "Cohere's own SDK sends no Accept header and reads the body as SSE. "
        "Streaming requests now send `Accept: text/event-stream`, and the "
        "reader also takes newline-delimited JSON and a whole-body JSON "
        "reply, so whichever framing that was, it is read rather than "
        "raised as an unrecognised shape.",
        "Where tolerance runs out the adapter RAISES CohereResponseShapeError "
        "rather than returning an empty string. An empty string is "
        "indistinguishable from a model that had nothing to say, and that "
        "ambiguity is the bug cohere_parse_client carried until 2026-09-15.",
    ),
)


CONTRACTS: tuple[WireContract, ...] = (CHAT_V2, PARSE)


def diff_report(report: dict[str, Any]) -> dict[str, Any]:
    """Diff a whole Cohere probe report against every contract here.

    The same shape ``bedrock_wire.diff_report`` returns, over a different
    contract set, so the two probes' reports can be read side by side and
    ``aws-preflight.sh`` can treat them alike. ``diff_section`` itself is
    imported rather than reimplemented — two copies of a diffing rule is how
    the two contracts start disagreeing about what "confirmed" means.

    Callable on a report loaded from disk, so a committed report can be
    re-diffed after an adapter changes without spending another live call.
    """
    per_call = {c.name: diff_section(c, report.get(c.probe_section)) for c in CONTRACTS}
    observed = [n for n, d in per_call.items() if d.get("status") == "observed"]
    broken = {n: d["required_missing"] for n, d in per_call.items() if d.get("required_missing")}
    undeclared = {n: d["undeclared"] for n, d in per_call.items() if d.get("undeclared")}
    return {
        "calls": per_call,
        "calls_observed": sorted(observed),
        "calls_not_observed": sorted(c.name for c in CONTRACTS if c.name not in observed),
        "required_fields_missing": broken,
        "undeclared_fields": undeclared,
        # Deliberately NOT called "passed". A diff with nothing observed is
        # not a pass, and this migration has already shipped one gate that
        # reported success over a report containing nothing but 403s.
        "contract_holds": bool(observed) and not broken,
    }
