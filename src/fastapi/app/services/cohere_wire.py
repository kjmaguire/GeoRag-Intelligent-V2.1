"""The Cohere API wire contract, as data (ADR-0023).

The sibling of ``bedrock_wire.py``, for the capabilities that left
Bedrock: chat (Command A+) and OCR (Parse 5) on 2026-09-15, and dense
embedding (Embed 5) on 2026-10-04. The first two are AWS Marketplace
SageMaker packages rather than Bedrock models, on instance classes that bill
while idle, so they moved to Cohere's own API; Embed 5 is not on Bedrock at
all (ADR-0025). Rerank stays on Bedrock and stays in ``bedrock_wire``.

Everything ``bedrock_wire``'s docstring says about WHY a contract-as-data is
worth having applies here unchanged, and is not repeated: read that module
first. The machinery — ``Field``, ``Status``, ``WireContract``,
``diff_section`` — is imported from it rather than duplicated, because two
copies of a diffing rule is how the two contracts start disagreeing about
what "confirmed" means.

One status value does NOT appear here, and its absence is the point.
``CARRIED`` means "observed on Azure AI Foundry by a live call on
2026-07-30, assumed to survive the host change". Chat has three of those on
Bedrock. Parse has none, on any host.

``OBSERVED_COHERE`` fields (promoted 2026-09-29, VEN-7) are exactly what the
committed ``cohere_probe_20260924T060435Z.json`` shows: the chat request and
reply on api.cohere.com, and Parse's TEXT path -- the string-form
``document.image_url`` accepted, ``pages[].blocks[].text.content`` in blocks
mode, ``pages[].markdown`` in markdown mode. That run's page held text only.
Parse's TABLE and FIGURE payloads -- the reason ADR-0019 chose Parse over
tesseract -- have NEVER been exercised on any host, and every field
describing them stays assumed or tolerated. The diff reports them as
``not_exercised``, not ``contradicted``: a text page cannot show a table.

``EMBED`` has NO observed fields at all: Embed 5 shipped on 2026-09-30 and no
probe run has called it. Its fields are carried from the Embed v4 contract
(``bedrock_wire.EMBED_TEXT`` / ``EMBED_IMAGE``, where the TEXT half was
observed on Bedrock on 2026-09-16) and from Cohere's published API, which is
a better guess than none and not a measurement. ``cohere_probe.probe_embed``
is what turns it into a diff.

For chat, the three Foundry observations are recorded on ``CHAT_CONVERSE``
in ``bedrock_wire`` and they do not carry a second time: a different host is
a second chance to be wrong in the same way. ``CHAT_V2`` below re-asks all
three.
"""

from __future__ import annotations

from typing import Any

from app.services.bedrock_wire import Field, Status, WireContract, diff_section

__all__ = ["CHAT_V2", "CONTRACTS", "COHERE_REPORT", "EMBED", "PARSE", "diff_report"]

#: The committed Cohere probe report the OBSERVED_COHERE fields below cite.
COHERE_REPORT = "cohere_probe_20260924T060435Z.json"
_OBS = Status.OBSERVED_COHERE


# ---------------------------------------------------------------------------
# Parse — POST {COHERE_BASE_URL}/v2/parse
# ---------------------------------------------------------------------------
# Until 2026-09-24 never empirically verified on any host. The committed
# cohere_probe_20260924T060435Z.json then exercised the TEXT path in both
# output formats (fields marked OBSERVED_COHERE below). Tables and figures
# were not in the sample page and remain unverified everywhere.
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
            _OBS,
            True,
            "COHERE_PARSE_MODEL — 'parse-v5.0' by default. A plain model name; "
            "there is no endpoint indirection on this host. Accepted live, "
            "although /v1/models did not list it (parse_model_listed: false).",
            report=COHERE_REPORT,
        ),
        Field("document.type", _OBS, True, "Literal 'image_url'.", report=COHERE_REPORT),
        Field(
            "document.image_url",
            _OBS,
            True,
            "One page as a data: URI, as a bare STRING. Until 2026-09-23 this "
            "was declared as `document.image_url.url` — chat's image object — "
            'and the first live call was refused with HTTP 400 "parameter '
            "'document.image_url' is of type object but should be of type "
            'string". The string form was accepted on 2026-09-24 (HTTP 200 in '
            "both output formats and on every pixel-ladder rung up to "
            "19,996,997 px / 448,863 bytes -- a SYNTHETIC page; the byte limit "
            "for a real scan is unprobed). Oversized plan sheets are "
            "DOWNSCALED, not tiled — Parse returns no word polygons to stitch "
            "tiles with.",
            report=COHERE_REPORT,
        ),
        Field("output_format", _OBS, True, "'blocks' and 'markdown' both accepted.", report=COHERE_REPORT),
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
        Field(
            "pages[]",
            _OBS,
            True,
            "One entry; only pages[0] is read. Top-level keys seen: id, meta, pages.",
            report=COHERE_REPORT,
        ),
        Field(
            "pages[].index",
            _OBS,
            False,
            "Zero-based page index, per the Cohere SDK's ParsePage. Nothing reads it: one request is one page.",
            evidence_key="index",
            report=COHERE_REPORT,
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
            _OBS,
            False,
            "'text' | 'table' | 'image'; defaults to text. Only 'text' has "
            "been seen -- 'table' and 'image' never exercised.",
            evidence_key="type",
            report=COHERE_REPORT,
        ),
        Field(
            "pages[].blocks[].text",
            _OBS,
            False,
            "In the SDK shape, the text block's payload OBJECT; in the flat "
            "one, the text itself. _first_str takes only a string, so the "
            "object's repr can never become page text again. Seen as the "
            "SDK-shape object (block keys {text, type}, payload {content}).",
            evidence_key="text",
            report=COHERE_REPORT,
        ),
        Field(
            "pages[].blocks[].text.content",
            _OBS,
            False,
            "The text, in the SDK shape — first spelling tried. The adapter "
            "read 275 chars from it live.",
            evidence_key="content",
            report=COHERE_REPORT,
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
            _OBS,
            False,
            "Markdown mode. The KEY was observed in page0_keys and the adapter "
            "read 275 chars; whether it arrived as a bare string or as an "
            "object (pages[].markdown.content) was not recorded -- "
            "_page_from_markdown takes both.",
            evidence_key="markdown",
            report=COHERE_REPORT,
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
        Field(
            "model",
            _OBS,
            True,
            "COHERE_CHAT_MODEL — 'command-a-plus-05-2026'. Accepted, and listed by /v1/models.",
            report=COHERE_REPORT,
        ),
        Field(
            "messages[].role",
            _OBS,
            True,
            "System is a MESSAGE here, not a top-level parameter. That is the "
            "opposite of Bedrock Converse and it fails SILENTLY when reversed: "
            "a system prompt sent as a user turn still returns fluent text, "
            "just without the grounding rules applied. role:'system' was "
            "accepted AND obeyed live (system_is_a_message.obeyed: true).",
            report=COHERE_REPORT,
        ),
        Field("messages[].content", _OBS, True, "Plain string per message.", report=COHERE_REPORT),
        Field("temperature", _OBS, True, "Caller's value, unmodified.", report=COHERE_REPORT),
        Field(
            "max_tokens",
            _OBS,
            True,
            "Capped by llm_common.cap_output_tokens so prompt + output cannot "
            "overflow COHERE_CHAT_MAX_MODEL_LEN. Providers answer 400 rather "
            "than truncating, so an uncapped request fails AFTER paying to "
            "build the prompt. The overflow 400 itself is not probed.",
            report=COHERE_REPORT,
        ),
        Field(
            "stream",
            _OBS,
            True,
            "True exactly when a token_callback was supplied. Both values sent "
            "live; stream=true answered text/event-stream with text on "
            "delta.message.content.text.",
            report=COHERE_REPORT,
        ),
        Field(
            "response_format.type",
            _OBS,
            False,
            "'json_object', sent only when the caller asks for JSON. Unlike "
            "Converse, which has no first-class JSON mode and rides a "
            "passthrough field, this is a documented top-level parameter. "
            "ACCEPTED live and the reply parsed as JSON -- but the run "
            "WITHOUT it also returned JSON (the prompt asked for it), so that "
            "report does not distinguish 'honoured' from 'ignored'. Hard rule "
            "4 depends on it being honoured; that part is still unproven.",
            report=COHERE_REPORT,
        ),
    ),
    response=(
        Field(
            "message.content[].type",
            _OBS,
            False,
            "Block discriminator, 'text' for the answer. Declared because a "
            "live-shaped run reported it as an UNDECLARED field — which is "
            "the discovery half of this contract working, on the first "
            "exercise of it.",
            evidence_key="type",
            report=COHERE_REPORT,
        ),
        Field(
            "message.content[].text",
            _OBS,
            True,
            "The answer, concatenated across typed blocks.",
            evidence_key="text",
            report=COHERE_REPORT,
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
            _OBS,
            False,
            "Where reasoning lands on this host — question (2) above. The "
            "2026-09-23 run from inside the VPC saw content blocks keyed "
            "{type, text, thinking} and no `reasoning_content` sibling (which "
            "this field used to declare, and which that run reported as "
            "contradicted). Reasoning is on by default and counts against "
            "max_tokens; a reply that is all thinking is a budget outcome "
            '(_extract_content returns ""), not an unreadable shape.',
            evidence_key="thinking",
            report=COHERE_REPORT,
        ),
        Field(
            "message.role",
            _OBS,
            False,
            "'assistant'. Nothing reads it; declared because the same run reported it as undeclared.",
            evidence_key="role",
            report=COHERE_REPORT,
        ),
        Field(
            "usage.tokens.input_tokens",
            _OBS,
            False,
            "v2 nests real counts under `tokens`. Seen beside output_tokens "
            "and reasoning_tokens (the last not accounted anywhere yet).",
            report=COHERE_REPORT,
        ),
        Field("usage.tokens.output_tokens", _OBS, False, "Same.", report=COHERE_REPORT),
        Field(
            "usage.billed_units",
            _OBS,
            False,
            "The billing view, which can differ from the token counts. "
            "_extract_usage prefers `tokens` and falls back to the flat shape. "
            "Present live beside `tokens` and `cached_tokens` (the latter not "
            "read -- see VEN-12).",
            report=COHERE_REPORT,
        ),
    ),
    notes=(
        "Question (3), the sentinels: cohere_probe_20260924T060435Z.json saw "
        "NONE in either JSON reply (sentinels_present: []). On this host "
        "clean_model_text's stripping is therefore not load-bearing -- kept, "
        "because it costs nothing and one run is not a guarantee.",
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

# ---------------------------------------------------------------------------
# Embed — POST {COHERE_BASE_URL}/v2/embed (ADR-0025)
# ---------------------------------------------------------------------------
# Embed 5 (`embed-v5.0-pro`, and `embed-v5.0-fast` for the later query-side
# move) on Cohere's own API. One contract for text AND image: the request is
# the same endpoint and the same response key, and the probe's ``embed``
# section records top-level keys for each variant under one evidence path
# list. NOTHING here has been observed -- every field is ASSUMED, including
# the ones the v4 text path confirmed on Bedrock, because a new model on a new
# host is a new chance to be wrong in the same way (the Foundry lesson).
#
# What the adapter would get wrong silently if an assumption fails:
#   * `output_dimension` ignored -> wrong-width vectors into a 1024-dim
#     collection (a 400 on every upsert, or a query that cannot match).
#   * `input_type` accepted but not honoured -> query and document vectors in
#     one subspace; retrieval degrades with every guard still passing.
#   * the per-request text limit below 96 -> every ingest batch is refused.
#   * which image shape is accepted -> every page image is skipped (and
#     retried every sweep) while text embeds fine.

EMBED = WireContract(
    name="embed",
    service="cohere-api",
    method="POST /v2/embed",
    probe_section="embed",
    # The probe records the reply's top-level keys per variant. The text
    # document call sits at the section root so a report is read by the same
    # path whichever variant ran; `query` and `image` are nested.
    evidence_paths=("top_level_keys", "query.top_level_keys", "image.top_level_keys"),
    request=(
        Field(
            "model",
            Status.ASSUMED,
            True,
            "COHERE_EMBED_MODEL ('embed-v5.0-pro') for ingest; "
            "COHERE_EMBED_QUERY_MODEL for questions, which defaults to the "
            "same model. 'embed-v5.0-fast' is the later, measured query-side "
            "move: Cohere says Pro and Fast share an embedding space and the "
            "probe's cross_model section measures it. Names are Cohere's "
            "2026-09-30 publication, not observed from this account.",
        ),
        Field(
            "texts[]",
            Status.ASSUMED,
            True,
            "The inputs, at most COHERE_EMBED_MAX_TEXTS_PER_CALL (96, carried "
            "over from v4) per request; _CohereEmbedding._post chunks to it. "
            "The probe's input_limit ladder sends 96 and 97.",
        ),
        Field(
            "input_type",
            Status.ASSUMED,
            True,
            "'search_document' for corpus chunks, 'search_query' for "
            "questions, 'image' for page renders. The query value is the "
            "one a missing `embed_query` method would silently lose "
            "(tools.py's hasattr fallback); test_embedding_cohere.py drives "
            "the real call site.",
        ),
        Field("embedding_types[]", Status.ASSUMED, True, "Always ['float']."),
        Field(
            "output_dimension",
            Status.ASSUMED,
            True,
            "COHERE_EMBED_DIMENSION, 1024, matching georag_chunks. Embed 5 Pro "
            "is documented at 2048/1536/1024/768/512/256. Whether 'fast' "
            "honours the same set is unobserved. A SILENTLY IGNORED dimension "
            "is the worst outcome in this contract; the adapter rejects a "
            "reply whose width differs, and the probe records "
            "dimension_honoured.",
        ),
        Field(
            "images[]",
            Status.ASSUMED,
            False,
            "PRIMARY image shape: a single data: URI, one image per call. "
            "Text and image inputs cannot be combined in one request.",
        ),
        Field(
            "inputs[].content[].type",
            Status.ASSUMED,
            False,
            "FALLBACK image shape ('inputs' with an image_url content part). "
            "Tried once, only on an HTTP 400/422, as the Bedrock adapter did "
            "on ValidationException. Which shape Embed 5 accepts is "
            "unobserved (ADR-0025 gotcha 4); collapse to the winner once a "
            "run says.",
        ),
        Field("inputs[].content[].image_url.url", Status.ASSUMED, False, "Same fallback."),
    ),
    response=(
        Field(
            "embeddings.float[][]",
            Status.ASSUMED,
            True,
            "Row-per-input float vectors, read straight into np.float32. The "
            "top-level `embeddings` key was seen on Embed v4 (Bedrock, "
            "2026-09-16) with ['float'] beneath it; not on Embed 5.",
            evidence_key="embeddings",
        ),
        Field(
            "id",
            Status.ASSUMED,
            False,
            "Request id. Nothing reads it; declared so a field seen on v4 is "
            "not reported as a fresh discovery.",
            evidence_key="id",
        ),
        Field("texts[] (echo)", Status.ASSUMED, False, "The inputs echoed back on a text call. Nothing reads it.", evidence_key="texts"),
        Field("images[] (echo)", Status.ASSUMED, False, "Likely on an image call. Nothing reads it.", evidence_key="images"),
        Field("response_type", Status.ASSUMED, False, "'embeddings_by_type' on v4. Nothing reads it.", evidence_key="response_type"),
        Field("meta", Status.ASSUMED, False, "api_version / billed_units. Nothing reads it.", evidence_key="meta"),
    ),
    notes=(
        "Authentication is `Authorization: bearer $COHERE_API_KEY`, the same "
        "key as chat and Parse (ADR-0023). A key that covers chat does not "
        "prove it covers embed; the probe's embed section is the check.",
        "A refused call produces NO AWS metric -- CloudWatch cannot see a "
        "request that never went to AWS -- so the adapter's own warning log "
        "is the whole signal. 429s are retried with Retry-After honoured "
        "(llm_common.parse_retry_after); the limit is per KEY, shared with "
        "chat and Parse (ADR-0025 gotcha 5).",
        "Vectors are ASSUMED pre-normalised (as on Bedrock v4), so "
        "normalize_embeddings is ignored. Qdrant's Cosine distance does not "
        "depend on it, but the probe records the L2 norm.",
    ),
)


CONTRACTS: tuple[WireContract, ...] = (CHAT_V2, PARSE, EMBED)


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
