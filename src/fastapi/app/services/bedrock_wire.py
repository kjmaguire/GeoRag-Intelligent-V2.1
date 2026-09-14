"""The Bedrock wire contract, as data (ADR-0022).

Every Bedrock adapter in this service says ``[UNVERIFIED]`` at the top,
because none of them has been confirmed against a live endpoint. Those
notices are prose, one per module, and prose cannot be diffed against a
probe report. This module is the same claims expressed as **data**: for each
of the six calls, exactly which fields the adapter sends and which it reads
back, and — the part that matters — how much we actually know about each one.

It does NOT verify anything. Nothing here is evidence. What it buys is that
the first credentialed run of ``ops/validation/bedrock_probe.py`` becomes a
**diff** rather than a discovery: ``diff_report()`` walks a report against
these declarations and says, per field, confirmed / contradicted / not
observed — plus ``undeclared``, which is how you find a shape nobody wrote
down. Correcting an adapter afterwards is then a list of named fields
instead of a careful re-read of five modules.

----------------------------------------------------------------------------
WHY THE STATUS FIELD IS THE POINT
----------------------------------------------------------------------------
"Unverified" is not one thing, and flattening it is how this migration would
lose the most. Three genuinely different situations:

  ASSUMED    Written from Bedrock's or Cohere's documentation. Never observed
             on any host, by anyone. Most of this file.

  CARRIED    Observed on Azure AI Foundry by a live call on 2026-07-30, and
             assumed — not known — to survive the host change. There are
             exactly three of these and they are the reason the probe exists:
             documentation got all three WRONG on Foundry (the catalog table
             said "Text only" response formats for Command A+ while Cohere's
             own docs listed it under Structured Outputs), and only a real
             request settled it. A second host is a second chance to be
             wrong in the same way.

  TOLERATED  An alternate shape the adapter accepts defensively. Not expected
             to be the winner. Its job is that a wrong guess degrades instead
             of crashing, so each one is a place where the code is
             deliberately not committing. The probe's job is to collapse
             these: once a live run says which shape arrives, delete the
             loser rather than carrying both forever.

No field is ``OBSERVED`` yet. That value exists so a real report can promote
one, and so the absence is countable rather than rhetorical.

----------------------------------------------------------------------------
THIS FILE IS A CLAIM ABOUT THE ADAPTERS, AND IS TESTED AS ONE
----------------------------------------------------------------------------
``tests/test_bedrock_wire_contract.py`` asserts that each adapter really does
build exactly the request declared here — no missing field, no undeclared
extra — and really does parse a response assembled from these declarations.
So the file cannot quietly drift from the code it describes: changing one
without the other fails CI. That is the only thing here that is actually
verified, and it is worth being precise about what it proves — that the code
and this description agree, not that either matches Bedrock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "CONTRACTS",
    "Field",
    "Status",
    "WireContract",
    "declared_response_keys",
    "diff_report",
    "diff_section",
]


class Status(StrEnum):
    """How much is actually known about a field. See the module docstring.

    StrEnum so the values serialise straight into the probe report without a
    custom encoder — this ends up inside a committed JSON artifact.
    """

    ASSUMED = "assumed"
    CARRIED = "carried_from_foundry"
    TOLERATED = "tolerated_alternate"
    OBSERVED = "observed_on_bedrock"


@dataclass(frozen=True)
class Field:
    """One field of a request or response.

    ``path`` is dotted, with ``[]`` marking a list whose elements the path
    continues into — ``output.message.content[].text`` means "the ``text``
    key of an element of ``output.message.content``". It is a description,
    not an accessor: nothing in this module evaluates it against live JSON.

    ``evidence_key`` is the bare key name as it would appear in one of the
    probe's observed key lists (``content_block_keys``, ``delta_keys``, …).
    A field without one is not something the current probe looks for, and
    ``diff_section`` reports it as ``not_observed`` rather than pretending.
    """

    path: str
    status: Status
    required: bool
    note: str
    evidence_key: str | None = None


@dataclass(frozen=True)
class WireContract:
    """One Bedrock call: what we send, what we read, where to look for proof."""

    name: str
    service: str
    method: str
    request: tuple[Field, ...]
    response: tuple[Field, ...]
    #: Section of the probe report that would evidence this call.
    probe_section: str
    #: Dotted paths INSIDE that section whose values are lists of observed
    #: key names. Two levels of ``*`` are supported by ``_resolve``: the chat
    #: section keys its results by request variant, so the path has to fan
    #: out over them rather than name them.
    evidence_paths: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# 1 + 2. Chat — bedrock-runtime Converse / ConverseStream
# ---------------------------------------------------------------------------
# The three CARRIED fields below are Foundry behaviours (1), (2) and (3) from
# llm_bedrock.py's docstring. Each is handled differently on purpose:
#   (1) response_format became additionalModelRequestFields, which Bedrock
#       forwards to the provider untouched. Whether Command A+ honours it
#       through a Marketplace endpoint is the single most important open
#       question in this file — every typed-output guard in
#       orchestrator_validators.py depends on the model returning JSON
#       (hard rule 4).
#   (2) reasoning has TWO declared shapes: Converse's own reasoningContent
#       block, and the Foundry-era sibling field, because a Marketplace
#       endpoint may pass the provider response through more literally than
#       a first-party model does.
#   (3) the sentinels are stripped unconditionally, so this contract does not
#       care which way it goes — but the probe still records it, because a
#       runtime that strips them means _clean() is dead code rather than a
#       load-bearing guard.

_CHAT_REQUEST: tuple[Field, ...] = (
    Field("modelId", Status.ASSUMED, True, "Marketplace endpoint ARN, or a serverless model id."),
    Field("messages[].role", Status.ASSUMED, True, "Always 'user'; there is one turn."),
    Field("messages[].content[].text", Status.ASSUMED, True, "The composed user message."),
    Field(
        "system[].text",
        Status.ASSUMED,
        False,
        "Top-level parameter, NOT a role:'system' message. Sent only when a "
        "system prompt exists. Getting this wrong fails silently — a system "
        "message delivered as a user turn still produces plausible output.",
    ),
    Field("inferenceConfig.maxTokens", Status.ASSUMED, True, "Output cap after cap_output_tokens()."),
    Field("inferenceConfig.temperature", Status.ASSUMED, True, "Caller's temperature."),
    Field(
        "additionalModelRequestFields.response_format.type",
        Status.CARRIED,
        False,
        "Foundry behaviour (1). Sent only for response_format='json_object'. "
        "Converse has no first-class JSON mode; this rides the passthrough.",
    ),
)

CHAT_CONVERSE = WireContract(
    name="chat_converse",
    service="bedrock-runtime",
    method="converse",
    probe_section="chat",
    evidence_paths=("*.content_block_keys", "*.message_sibling_keys"),
    request=_CHAT_REQUEST,
    response=(
        Field(
            "output.message.content[].text",
            Status.ASSUMED,
            True,
            "The answer. Concatenated across blocks.",
            evidence_key="text",
        ),
        Field(
            "output.message.content[].reasoningContent.reasoningText.text",
            Status.ASSUMED,
            False,
            "Converse's own representation of reasoning. Never forwarded to "
            "the user; read only to tell an empty answer apart from a "
            "budget exhausted by thinking.",
            evidence_key="reasoningContent",
        ),
        Field(
            "output.message.content[].reasoningContent.text",
            Status.TOLERATED,
            False,
            "Flatter variant of the same block. Accepted because the nesting "
            "is not worth a crash.",
        ),
        Field(
            "output.message.reasoning_content",
            Status.CARRIED,
            False,
            "Foundry behaviour (2) — reasoning as a SIBLING of content, not a "
            "block. Kept because a Marketplace endpoint forwards the "
            "provider's own response more literally than a first-party model.",
            evidence_key="reasoning_content",
        ),
        Field(
            "output.message.reasoning",
            Status.TOLERATED,
            False,
            "Third spelling of the same thing.",
            evidence_key="reasoning",
        ),
        Field("stopReason", Status.ASSUMED, False, "'max_tokens' is the budget-exhausted signal."),
        Field("usage.inputTokens", Status.ASSUMED, False, "Defaults to 0; cost accounting only."),
        Field("usage.outputTokens", Status.ASSUMED, False, "Defaults to 0."),
        Field(
            "usage.cacheReadInputTokens",
            Status.ASSUMED,
            False,
            "Prompt caching. Command A+ on a Marketplace endpoint may report "
            "neither token key; both default to 0 rather than being assumed.",
        ),
    ),
    notes=(
        "Foundry behaviour (3): Cohere wraps JSON output in "
        "<|START_TEXT|>/<|END_TEXT|>. _clean() strips them unconditionally, so "
        "either outcome is safe — but if the runtime strips them first, that "
        "guard is dead code rather than load-bearing, and the probe records "
        "which it is (chat.*.sentinels_present).",
    ),
)

CHAT_CONVERSE_STREAM = WireContract(
    name="chat_converse_stream",
    service="bedrock-runtime",
    method="converse_stream",
    probe_section="chat_stream",
    evidence_paths=("distinct_event_kinds", "delta_keys"),
    request=_CHAT_REQUEST,
    response=(
        Field(
            "stream[].contentBlockDelta.delta.text",
            Status.ASSUMED,
            True,
            "One streamed token run. Forwarded to token_callback BEFORE being "
            "buffered, so a callback that raises cannot leave the buffer "
            "ahead of what the user saw.",
            evidence_key="text",
        ),
        Field(
            "stream[].contentBlockDelta.delta.reasoningContent.text",
            Status.ASSUMED,
            False,
            "Reasoning delta. Buffered, never forwarded.",
            evidence_key="reasoningContent",
        ),
        Field(
            "stream[].contentBlockDelta.delta.reasoningContent.reasoningText.text",
            Status.TOLERATED,
            False,
            "Nested variant of the same delta.",
        ),
        Field(
            "stream[].messageStop.stopReason",
            Status.ASSUMED,
            False,
            "Where stopReason arrives in the streaming shape.",
            evidence_key="messageStop",
        ),
        Field(
            "stream[].metadata.usage",
            Status.ASSUMED,
            False,
            "Usage arrives once, at the end, in its own event.",
            evidence_key="metadata",
        ),
        Field(
            "stream[].contentBlockDelta",
            Status.ASSUMED,
            True,
            "The event kind carrying content at all.",
            evidence_key="contentBlockDelta",
        ),
    ),
)


# ---------------------------------------------------------------------------
# 3 + 4. Embeddings — bedrock-runtime InvokeModel
# ---------------------------------------------------------------------------
# The body is Cohere's own v2 schema minus `model`, which moves to modelId.
# That is why the embed adapter is a transport swap and not a rewrite.

_EMBED_RESPONSE: tuple[Field, ...] = (
    Field(
        "embeddings.float[][]",
        Status.ASSUMED,
        True,
        "Row-per-input float vectors. Read straight into np.float32.",
    ),
)

EMBED_TEXT = WireContract(
    name="embed_text",
    service="bedrock-runtime",
    method="invoke_model",
    probe_section="embed",
    request=(
        Field("modelId", Status.ASSUMED, True, "BEDROCK_EMBED_MODEL_ID."),
        Field("body.texts[]", Status.ASSUMED, True, "The inputs."),
        Field(
            "body.input_type",
            Status.ASSUMED,
            True,
            "'search_document' for corpus chunks, 'search_query' at query "
            "time. Cohere's asymmetric embedding is a real quality lever the "
            "plain SentenceTransformer interface has no slot for.",
        ),
        Field("body.embedding_types[]", Status.ASSUMED, True, "Always ['float']."),
        Field(
            "body.output_dimension",
            Status.ASSUMED,
            True,
            "1024, matching georag_chunks. A SILENTLY IGNORED dimension is the "
            "worst outcome in this file: 1536-dim vectors written into a "
            "1024-dim collection, discovered at query time.",
        ),
    ),
    response=_EMBED_RESPONSE,
)

EMBED_IMAGE = WireContract(
    name="embed_image",
    service="bedrock-runtime",
    method="invoke_model",
    probe_section="embed",
    request=(
        Field("modelId", Status.ASSUMED, True, "BEDROCK_EMBED_MODEL_ID."),
        Field(
            "body.images[]",
            Status.ASSUMED,
            True,
            "PRIMARY shape: a single data: URI. One image per call — a page "
            "render is 1-3 MB and batching multiplies the body for no latency "
            "win (cf. the 2026-08-07 SPLADE batching OOM, exit 137).",
        ),
        Field(
            "body.inputs[].content[].type",
            Status.TOLERATED,
            False,
            "FALLBACK shape: Cohere shipped two accepted bodies for v4 and "
            "which one a given host accepts is undocumented. Tried once, only "
            "on a ValidationException — any other error means the request was "
            "understood and re-shaping just burns a call.",
        ),
        Field("body.inputs[].content[].image_url.url", Status.TOLERATED, False, "Same fallback."),
        Field("body.input_type", Status.ASSUMED, True, "'image'. Text and image inputs cannot be combined."),
        Field("body.embedding_types[]", Status.ASSUMED, True, "Always ['float']."),
        Field("body.output_dimension", Status.ASSUMED, True, "1024 — the SAME space as text vectors."),
    ),
    response=_EMBED_RESPONSE,
    notes=(
        "Which body shape wins is recorded on first success and reported in "
        "the logs. Collapse this contract to the winner and delete the loser "
        "once a live run says which — carrying both forever is the cost of "
        "not knowing, not a feature.",
    ),
)


# ---------------------------------------------------------------------------
# 5. Rerank — bedrock-agent-runtime Rerank
# ---------------------------------------------------------------------------
# The one adapter whose request genuinely CHANGED rather than moving hosts.
# Cohere's /v2/rerank body did not survive: this is Bedrock's own API, on a
# different client, and the response field is camelCase.

RERANK = WireContract(
    name="rerank",
    service="bedrock-agent-runtime",
    method="rerank",
    probe_section="rerank",
    request=(
        Field("queries[].type", Status.ASSUMED, True, "Literal 'TEXT'."),
        Field("queries[].textQuery.text", Status.ASSUMED, True, "One query per call."),
        Field("sources[].type", Status.ASSUMED, True, "Literal 'INLINE'."),
        Field("sources[].inlineDocumentSource.type", Status.ASSUMED, True, "Literal 'TEXT'."),
        Field("sources[].inlineDocumentSource.textDocument.text", Status.ASSUMED, True, "One candidate."),
        Field(
            "rerankingConfiguration.type",
            Status.ASSUMED,
            True,
            "Literal 'BEDROCK_RERANKING_MODEL'.",
        ),
        Field(
            "rerankingConfiguration.bedrockRerankingConfiguration.modelConfiguration.modelArn",
            Status.ASSUMED,
            True,
            "Rerank 3.5 — NOT v4, which Bedrock does not serve.",
        ),
        Field(
            "rerankingConfiguration.bedrockRerankingConfiguration.numberOfResults",
            Status.ASSUMED,
            True,
            "Set to the FULL document count so every candidate is scored back, "
            "not just the model's own top-N. A smaller value silently drops "
            "candidates to 0.0 and the only retrieval-quality gate in the "
            "system reads those scores.",
        ),
    ),
    response=(
        Field(
            "results[].index",
            Status.ASSUMED,
            True,
            "Position in the sources list. The scores are remapped through it "
            "back to the caller's pair order, so a wrong index is a silent "
            "scrambling rather than an error.",
        ),
        Field(
            "results[].relevanceScore",
            Status.ASSUMED,
            True,
            "camelCase, NOT relevance_score — the Cohere spelling does not "
            "survive the move to Bedrock's own API.",
        ),
    ),
)


# ---------------------------------------------------------------------------
# 6. Parse — bedrock-runtime InvokeModel
# ---------------------------------------------------------------------------
# NEVER EMPIRICALLY VERIFIED, on Foundry or on Bedrock. That is a stronger
# statement than the rest of this file: for the other five calls a previous
# host at least confirmed a related shape. Here nothing ever has, which is
# why the response side is almost entirely TOLERATED alternates — the
# adapter is guessing in three places at once and degrading rather than
# crashing is the whole design.

PARSE = WireContract(
    name="parse",
    service="bedrock-runtime",
    method="invoke_model",
    probe_section="parse",
    request=(
        Field("modelId", Status.ASSUMED, True, "BEDROCK_PARSE_MODEL_ID — a Marketplace endpoint."),
        Field("body.document.type", Status.ASSUMED, True, "Literal 'image_url'."),
        Field(
            "body.document.image_url.url",
            Status.ASSUMED,
            True,
            "One page as a data: URI. Oversized plan sheets are DOWNSCALED, "
            "not tiled — Parse returns no word polygons to stitch tiles with.",
        ),
        Field("body.output_format", Status.ASSUMED, True, "'blocks' or 'markdown'."),
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
    ),
)


CONTRACTS: tuple[WireContract, ...] = (
    CHAT_CONVERSE,
    CHAT_CONVERSE_STREAM,
    EMBED_TEXT,
    EMBED_IMAGE,
    RERANK,
    PARSE,
)


# ---------------------------------------------------------------------------
# Diffing a probe report against the contract
# ---------------------------------------------------------------------------


def declared_response_keys(contract: WireContract) -> set[str]:
    """Every ``evidence_key`` the contract declares for responses."""
    return {f.evidence_key for f in contract.response if f.evidence_key}


def _resolve(section: Any, path: str) -> list[list[str]]:
    """Collect the key-lists a dotted ``path`` names inside a report section.

    ``*`` fans out over a dict's values, which the chat section needs: it
    keys its results by request variant ('with_response_format', …) and the
    variant names are not something this module should have to know.

    Returns a list of lists so "the path resolved to nothing" and "the path
    resolved to an empty list" stay distinguishable — the first means the
    probe never got that far, the second means it looked and saw no keys.
    Reporting those as the same thing is exactly the kind of collapse this
    file exists to prevent.
    """
    nodes: list[Any] = [section]
    for part in path.split("."):
        nxt: list[Any] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if part == "*":
                nxt.extend(node.values())
            elif part in node:
                nxt.append(node[part])
        nodes = nxt
    return [[str(k) for k in n] for n in nodes if isinstance(n, list)]


def diff_section(contract: WireContract, section: Any) -> dict[str, Any]:
    """Compare one probe report section against one contract.

    Verdicts, per declared response field with an ``evidence_key``:

      confirmed    the probe saw that key
      contradicted the probe looked at a list that should have held it and
                   it was not there. For a ``required`` field this is the
                   adapter being wrong, not the contract.
      not_observed the probe never resolved a list to look in — no evidence
                   either way. The default, and the honest one, for a run
                   that could not authenticate.

    Plus ``undeclared``: keys the probe saw that nothing here declares. That
    is the discovery half — a field arriving that no adapter reads.
    """
    if not isinstance(section, dict) or "skipped" in section or "error" in section:
        return {
            "status": "not_observed",
            "reason": (section or {}).get("skipped")
            or (section or {}).get("error")
            or "section absent from report",
        }

    observed: set[str] = set()
    looked = False
    for path in contract.evidence_paths:
        for key_list in _resolve(section, path):
            looked = True
            observed.update(key_list)

    declared = declared_response_keys(contract)
    confirmed = sorted(declared & observed)
    missing = sorted(declared - observed) if looked else []
    required_keys = {
        f.evidence_key for f in contract.response if f.required and f.evidence_key
    }

    return {
        "status": "observed" if looked else "not_observed",
        "confirmed": confirmed,
        "contradicted": missing,
        "not_observed": sorted(declared) if not looked else [],
        "undeclared": sorted(observed - declared),
        # The one line an operator needs: a REQUIRED field the probe looked
        # for and did not find means the adapter reads something Bedrock does
        # not send, and the answer path is broken, not merely unverified.
        "required_missing": sorted(required_keys - observed) if looked else [],
    }


def diff_report(report: dict[str, Any]) -> dict[str, Any]:
    """Diff a whole probe report against every contract.

    Written to be callable on a report loaded from disk, so a committed
    report can be re-diffed after an adapter changes without re-running
    against AWS — which matters, because running against AWS costs a
    Marketplace endpoint and there is no staging account.
    """
    per_call = {c.name: diff_section(c, report.get(c.probe_section)) for c in CONTRACTS}
    observed_calls = [n for n, d in per_call.items() if d.get("status") == "observed"]
    broken = {
        n: d["required_missing"]
        for n, d in per_call.items()
        if d.get("required_missing")
    }
    undeclared = {n: d["undeclared"] for n, d in per_call.items() if d.get("undeclared")}
    return {
        "calls": per_call,
        "calls_observed": sorted(observed_calls),
        "calls_not_observed": sorted(c.name for c in CONTRACTS if c.name not in observed_calls),
        "required_fields_missing": broken,
        "undeclared_fields": undeclared,
        # Deliberately NOT called "passed". A diff with nothing observed is
        # not a pass, and this migration has already shipped one gate that
        # reported success over a report containing nothing but 403s.
        "contract_holds": bool(observed_calls) and not broken,
    }
