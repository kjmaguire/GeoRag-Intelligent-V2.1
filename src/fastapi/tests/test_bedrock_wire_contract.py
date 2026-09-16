"""The Bedrock adapters must send and read exactly what bedrock_wire declares.

``app/services/bedrock_wire.py`` is the wire contract as data, so that the
first credentialed probe run is a diff rather than a discovery. A contract
nothing checks is just a second place to be wrong — worse than none, because
it reads as authority. These tests are what stop that: each adapter's real
request is captured and compared field-for-field against the declaration, in
both directions, so neither side can move without the other.

What this proves and what it does not, because the distinction is the whole
point of the file: it proves THE CODE AND THE DESCRIPTION AGREE. It proves
nothing about Bedrock. Every field here is still ``[UNVERIFIED]`` and stays
that way until ``ops/validation/bedrock_probe.py`` runs with real credentials
(ADR-0022 "Verification").
"""

from __future__ import annotations

import json
from typing import Any

import pytest

# Parse left Bedrock on 2026-09-15 (ADR-0023) and its contract went with it,
# so PARSE now comes from cohere_wire. It is still exercised in this file
# rather than one of its own, because these tests are about the same property
# for every adapter — the code and the description agree — and splitting them
# by host would make that property two half-checks.
from app.services.bedrock_wire import (
    CHAT_CONVERSE,
    CONTRACTS,
    EMBED_IMAGE,
    EMBED_TEXT,
    RERANK,
    Field,
    Status,
    WireContract,
    diff_report,
    diff_section,
)
from app.services.cohere_wire import CHAT_V2, PARSE

# ---------------------------------------------------------------------------
# Turning a declaration into a set of concrete paths
# ---------------------------------------------------------------------------


def _paths(payload: Any, prefix: str = "") -> set[str]:
    """Flatten real JSON into the dotted/[] notation the contract uses.

    A list becomes ONE ``[]`` segment regardless of length: the contract
    describes a shape, not a size, and two documents in a rerank request are
    the same field as one.
    """
    out: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            here = f"{prefix}.{key}" if prefix else key
            child = _paths(value, here)
            out |= child or {here}
    elif isinstance(payload, list):
        here = f"{prefix}[]"
        for item in payload:
            child = _paths(item, here)
            out |= child or {here}
        if not payload:
            out.add(here)
    return out


def _declared(contract: WireContract, *, side: str, optional: bool) -> set[str]:
    fields: tuple[Field, ...] = getattr(contract, side)
    return {
        f.path
        for f in fields
        if (optional or f.required) and f.status is not Status.TOLERATED
    }


def _assert_request_matches(contract: WireContract, sent: dict[str, Any]) -> None:
    """Every required field present, and nothing sent that is undeclared."""
    actual = _paths(sent)
    declared_all = {f.path for f in contract.request}

    missing = _declared(contract, side="request", optional=False) - actual
    assert not missing, (
        f"{contract.name}: the adapter does NOT send required field(s) "
        f"{sorted(missing)} that bedrock_wire declares. One of the two is wrong."
    )

    undeclared = actual - declared_all
    assert not undeclared, (
        f"{contract.name}: the adapter sends {sorted(undeclared)}, which "
        f"bedrock_wire does not declare. Add it there (with an honest Status) "
        f"or stop sending it — an undeclared field is one the probe will not "
        f"think to check."
    )


# ---------------------------------------------------------------------------
# 1 + 2. Chat
# ---------------------------------------------------------------------------


def _chat_request(**kwargs: Any) -> dict[str, Any]:
    from app.agent.llm_bedrock import _build_request

    base: dict[str, Any] = {
        "user_message": "What is the average grade?",
        "system_content": "You are a geologist.",
        "temperature": 0.1,
        "max_output": 512,
        "response_format": None,
    }
    base.update(kwargs)
    return _build_request(**base)


def test_converse_request_matches_the_contract() -> None:
    _assert_request_matches(CHAT_CONVERSE, _chat_request())


def test_converse_json_mode_sends_the_carried_foundry_field() -> None:
    """Foundry behaviour (1), the most important open question in the file.

    Every typed-output guard in orchestrator_validators.py depends on the
    model actually returning JSON (hard rule 4), and on Converse the only
    route to asking for it is the provider passthrough.
    """
    sent = _chat_request(response_format="json_object")
    _assert_request_matches(CHAT_CONVERSE, sent)
    assert sent["additionalModelRequestFields"] == {
        "response_format": {"type": "json_object"}
    }


def test_converse_omits_the_passthrough_when_not_asking_for_json() -> None:
    assert "additionalModelRequestFields" not in _chat_request()


def test_the_system_prompt_is_a_top_level_parameter_not_a_message() -> None:
    """A system message delivered as a user turn still produces plausible
    output, so this one cannot be caught by reading answers."""
    sent = _chat_request()
    assert sent["system"] == [{"text": "You are a geologist."}]
    assert [m["role"] for m in sent["messages"]] == ["user"]


def test_an_empty_system_prompt_sends_no_system_key() -> None:
    assert "system" not in _chat_request(system_content="")


@pytest.mark.parametrize(
    ("message", "expected_content", "expected_reasoning"),
    [
        pytest.param(
            {"content": [{"text": "answer"}]},
            "answer",
            "",
            id="plain_text_block",
        ),
        pytest.param(
            {
                "content": [
                    {"reasoningContent": {"reasoningText": {"text": "thinking"}}},
                    {"text": "answer"},
                ]
            },
            "answer",
            "thinking",
            id="converse_reasoning_block",
        ),
        pytest.param(
            {"content": [{"reasoningContent": {"text": "flat"}}, {"text": "a"}]},
            "a",
            "flat",
            id="tolerated_flat_reasoning_block",
        ),
        pytest.param(
            # Foundry behaviour (2). A Marketplace endpoint may forward the
            # provider's response more literally than a first-party model.
            {"content": [{"text": "answer"}], "reasoning_content": "sibling"},
            "answer",
            "sibling",
            id="carried_foundry_sibling_field",
        ),
        pytest.param(
            {"content": [{"text": "answer"}], "reasoning": "third-spelling"},
            "answer",
            "third-spelling",
            id="tolerated_reasoning_spelling",
        ),
    ],
)
def test_every_declared_reasoning_shape_is_actually_handled(
    message: dict, expected_content: str, expected_reasoning: str
) -> None:
    """Each TOLERATED alternate is a place the code refuses to commit.

    They earn their keep only if they work; an alternate that is declared and
    broken is worse than one that was never claimed.
    """
    from app.agent.llm_bedrock import _extract_from_message

    content, reasoning = _extract_from_message(message)
    assert content == expected_content
    assert reasoning == expected_reasoning


def test_the_converse_block_shape_wins_over_the_sibling_field() -> None:
    """Both present is not a coin toss: the block is Converse's own shape."""
    from app.agent.llm_bedrock import _extract_from_message

    _, reasoning = _extract_from_message(
        {
            "content": [{"reasoningContent": {"reasoningText": {"text": "block"}}}],
            "reasoning_content": "sibling",
        }
    )
    assert reasoning == "block"


def test_the_streaming_request_is_the_same_shape_as_the_unary_one() -> None:
    """converse and converse_stream share _build_request, and the contract
    says so by sharing the tuple. If they ever diverge, this fails."""
    assert CHAT_CONVERSE.request is not None
    from app.services.bedrock_wire import CHAT_CONVERSE_STREAM

    assert CHAT_CONVERSE_STREAM.request == CHAT_CONVERSE.request


# ---------------------------------------------------------------------------
# 3 + 4. Embeddings
# ---------------------------------------------------------------------------


def _capture_embed(monkeypatch: pytest.MonkeyPatch, call, *, response: Any = None):
    """Run ``call`` against a stubbed InvokeModel and return the body sent."""
    from app.services import embedding as embedding_module

    sent: dict[str, Any] = {}

    def _fake_invoke(self, body):  # noqa: ANN001
        sent.clear()
        sent.update({"modelId": self._model_id, "body": body})
        return response if response is not None else {
            "embeddings": {"float": [[0.0] * 1024]}
        }

    monkeypatch.setattr(embedding_module._BedrockEmbedding, "_invoke", _fake_invoke)
    call(embedding_module._BedrockEmbedding())
    return sent


def test_text_embed_request_matches_the_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _capture_embed(monkeypatch, lambda m: m.encode(["a chunk"]))
    _assert_request_matches(EMBED_TEXT, sent)


def test_the_output_dimension_is_always_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """A silently ignored dimension writes 1536-dim vectors into a 1024-dim
    collection and is only discovered at query time. Not asking at all would
    be strictly worse, and is the failure this pins."""
    sent = _capture_embed(monkeypatch, lambda m: m.encode(["a chunk"]))
    assert sent["body"]["output_dimension"] == 1024


def test_asymmetric_input_types_reach_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = _capture_embed(monkeypatch, lambda m: m.encode(["chunk"]))
    query = _capture_embed(monkeypatch, lambda m: m.embed_query("question"))
    assert corpus["body"]["input_type"] == "search_document"
    assert query["body"]["input_type"] == "search_query"


def test_image_embed_sends_the_primary_shape_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import embedding as embedding_module

    monkeypatch.setattr(embedding_module._BedrockEmbedding, "_IMAGE_WIRE_SHAPE", None)
    sent = _capture_embed(monkeypatch, lambda m: m.embed_image(b"\x89PNG fake"))
    assert "images" in sent["body"], (
        "the `images` body is the declared primary; the `inputs` body is the "
        "fallback tried once on a ValidationException"
    )
    _assert_request_matches(EMBED_IMAGE, sent)


def test_text_and_image_inputs_are_never_combined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cohere's model card forbids it outright, so this is a request the
    provider would reject rather than a quality question."""
    sent = _capture_embed(monkeypatch, lambda m: m.embed_image(b"\x89PNG fake"))
    assert "texts" not in sent["body"]
    assert sent["body"]["input_type"] == "image"


# ---------------------------------------------------------------------------
# 5. Rerank
# ---------------------------------------------------------------------------


def test_rerank_request_matches_the_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import _bedrock as bedrock_module
    from app.services import reranker as reranker_module

    sent: dict[str, Any] = {}

    class _FakeClient:
        def rerank(self, **kwargs):
            sent.clear()
            sent.update(kwargs)
            return {"results": [{"index": 0, "relevanceScore": 0.9},
                                {"index": 1, "relevanceScore": 0.1}]}

    monkeypatch.setattr(bedrock_module, "get_client", lambda *a, **k: _FakeClient())
    model = reranker_module._BedrockReranker("cohere.rerank-v3-5:0", timeout_s=5.0)
    monkeypatch.setattr(model, "_model_arn", lambda: "arn:aws:bedrock:::model/cohere.rerank-v3-5:0")

    scores = model.predict([("q", "relevant doc"), ("q", "irrelevant doc")])

    _assert_request_matches(RERANK, sent)
    assert scores == [0.9, 0.1]


def test_every_candidate_is_scored_back_not_just_the_models_top_n(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """numberOfResults below the document count silently drops candidates to
    0.0, and those scores are the only retrieval-quality gate in the system
    (hard rule 5, as built)."""
    from app.services import _bedrock as bedrock_module
    from app.services import reranker as reranker_module

    sent: dict[str, Any] = {}

    class _FakeClient:
        def rerank(self, **kwargs):
            sent.update(kwargs)
            return {"results": [{"index": i, "relevanceScore": 0.5} for i in range(5)]}

    monkeypatch.setattr(bedrock_module, "get_client", lambda *a, **k: _FakeClient())
    model = reranker_module._BedrockReranker("m", timeout_s=5.0)
    monkeypatch.setattr(model, "_model_arn", lambda: "arn:x")
    model.predict([("q", f"doc {i}") for i in range(5)])

    config = sent["rerankingConfiguration"]["bedrockRerankingConfiguration"]
    assert config["numberOfResults"] == 5


def test_scores_are_remapped_through_index_not_response_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong index is a silent scrambling, not an error — the pairs come
    back in whatever order the model ranked them."""
    from app.services import _bedrock as bedrock_module
    from app.services import reranker as reranker_module

    class _FakeClient:
        def rerank(self, **kwargs):
            # Deliberately returned best-first, which is NOT input order.
            return {"results": [{"index": 2, "relevanceScore": 0.9},
                                {"index": 0, "relevanceScore": 0.5},
                                {"index": 1, "relevanceScore": 0.1}]}

    monkeypatch.setattr(bedrock_module, "get_client", lambda *a, **k: _FakeClient())
    model = reranker_module._BedrockReranker("m", timeout_s=5.0)
    monkeypatch.setattr(model, "_model_arn", lambda: "arn:x")

    assert model.predict([("q", "a"), ("q", "b"), ("q", "c")]) == [0.5, 0.1, 0.9]


def test_the_response_field_is_camel_case() -> None:
    """Cohere's `relevance_score` does not survive the move to Bedrock's own
    Rerank API. Reading the wrong spelling would KeyError at query time."""
    assert any(f.path == "results[].relevanceScore" for f in RERANK.response)
    assert not any("relevance_score" in f.path for f in RERANK.response)


# ---------------------------------------------------------------------------
# 6. Parse
# ---------------------------------------------------------------------------


def test_parse_request_matches_the_contract() -> None:
    """``model`` is back in the body, which is the whole ADR-0023 change here.

    ``_request_body`` builds the document half and ``_invoke`` splices the
    model in, so the contract is checked against the assembled body — the
    same thing that goes on the wire — rather than against either half.
    """
    from app.services.ingest import cohere_parse_client as parse

    body = {"model": parse.parse_model(), **parse._request_body(b"\x89PNG fake")}
    _assert_request_matches(PARSE, body)
    assert body["document"]["image_url"]["url"].startswith("data:image/png;base64,")
    assert body["model"] == "parse-v5.0"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(
            {"pages": [{"blocks": [{"type": "text", "text": "hello"}]}]},
            "hello",
            id="text_block_first_spelling",
        ),
        pytest.param(
            {"pages": [{"blocks": [{"type": "text", "content": "hello"}]}]},
            "hello",
            id="tolerated_content_spelling",
        ),
        pytest.param(
            {"pages": [{"blocks": [{"type": "text", "markdown": "hello"}]}]},
            "hello",
            id="tolerated_markdown_spelling",
        ),
        pytest.param(
            {"pages": [{"markdown": "hello"}]},
            "hello",
            id="markdown_mode_bare_string",
        ),
        pytest.param(
            {"pages": [{"markdown": {"content": "hello"}}]},
            "hello",
            id="tolerated_markdown_object",
        ),
        pytest.param(
            {"blocks": [{"type": "text", "text": "hello"}]},
            "hello",
            id="tolerated_missing_pages_wrapper",
        ),
    ],
)
def test_every_declared_parse_shape_is_actually_handled(
    payload: dict, expected: str
) -> None:
    """Parse's contract has NEVER been verified on any host, which is why the
    adapter guesses in three places at once. Each guess has to work."""
    from app.services.ingest import cohere_parse_client as parse

    assert parse._page_from_payload(payload).text.strip() == expected


def test_an_unrecognised_parse_payload_degrades_rather_than_raising() -> None:
    """The right posture for a contract nobody has ever confirmed: a wrong
    guess costs the page to the Tesseract fallback, not the ingestion run."""
    from app.services.ingest import cohere_parse_client as parse

    result = parse._page_from_payload({"unexpected": "shape"})
    assert result.text == ""
    assert result.confidence_reported is False


# ---------------------------------------------------------------------------
# 7. Chat on Cohere's own API (ADR-0023)
# ---------------------------------------------------------------------------


def _cohere_chat_request(**kwargs: Any) -> dict[str, Any]:
    from app.agent.llm_cohere import _build_request

    base: dict[str, Any] = {
        "user_message": "What is the average grade?",
        "system_content": "You are a geologist.",
        "temperature": 0.1,
        "max_output": 512,
        "response_format": None,
        "stream": False,
    }
    base.update(kwargs)
    return _build_request(**base)


def test_cohere_chat_request_matches_the_contract() -> None:
    _assert_request_matches(CHAT_V2, _cohere_chat_request())


def test_the_cohere_system_prompt_is_a_message_not_a_top_level_field() -> None:
    """The mirror image of the Converse test above, and the same trap.

    Converse takes `system` as a top-level parameter; Cohere v2 takes it as a
    message. Each is silently wrong on the other host — a system prompt
    delivered as a user turn still produces fluent output, just without the
    grounding rules — and the generic path-set check cannot see it, because a
    system message and a user message are the same shape. That is why both
    hosts get a test written for this specifically.
    """
    body = _cohere_chat_request()
    assert "system" not in body, "that is the Bedrock Converse shape"
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][-1]["role"] == "user"


def test_the_cohere_json_mode_field_is_top_level() -> None:
    """Hard rule 4's dependency, on this host.

    Not `additionalModelRequestFields` — that is Converse's passthrough for a
    runtime with no first-class JSON mode. Cohere v2 documents this as a
    parameter of its own, which is a reason to expect it to work and not a
    reason to assume it does: the probe still asks.
    """
    body = _cohere_chat_request(response_format="json_object")
    assert body["response_format"] == {"type": "json_object"}
    assert "additionalModelRequestFields" not in body


def test_the_cohere_request_omits_json_mode_when_not_asked() -> None:
    assert "response_format" not in _cohere_chat_request()


def test_the_cohere_streaming_request_is_the_same_shape() -> None:
    """Only `stream` differs. A streaming path that quietly dropped a field
    would be a different contract wearing the same name."""
    unary = _cohere_chat_request(stream=False)
    streaming = _cohere_chat_request(stream=True)
    assert _paths(streaming) == _paths(unary)
    assert streaming["stream"] is True
    assert unary["stream"] is False


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(
            {"message": {"content": [{"type": "text", "text": "hello"}]}},
            "hello",
            id="documented_typed_blocks",
        ),
        pytest.param(
            {"message": {"content": "hello"}}, "hello", id="tolerated_bare_string"
        ),
        pytest.param({"text": "hello"}, "hello", id="tolerated_pre_v2_spelling"),
    ],
)
def test_every_declared_cohere_chat_shape_is_actually_handled(
    payload: dict, expected: str
) -> None:
    """Each TOLERATED alternate has to work. One that is declared and broken
    is worse than one never claimed."""
    from app.agent.llm_cohere import _extract_content

    assert _extract_content(payload) == expected


def test_an_unrecognised_cohere_chat_payload_raises_rather_than_returning_empty() -> None:
    """The one place this adapter is deliberately NOT tolerant.

    Parse degrades to tesseract, so a wrong guess there costs tables. Chat
    has no floor to fall to, and an empty string is indistinguishable from a
    model that had nothing to say — the ambiguity cohere_parse_client carried
    until 2026-09-15.
    """
    from app.agent.llm_cohere import CohereResponseShapeError, _extract_content

    with pytest.raises(CohereResponseShapeError):
        _extract_content({"unexpected": "shape"})


# ---------------------------------------------------------------------------
# The contract's own invariants
# ---------------------------------------------------------------------------


def test_nothing_claims_to_have_been_observed_on_bedrock() -> None:
    """The moment this fails, someone has run the probe and promoted a field.

    That is a good failure — but it must be a deliberate one, accompanied by
    a committed report, not a status that drifted upward while nobody was
    looking. ADR-0022 makes that report the gate on trusting any adapter, and
    ADR-0023 keeps it: the host changed, the absence of evidence did not.
    """
    observed = [
        (c.name, f.path)
        for c in (*CONTRACTS, PARSE, CHAT_V2)
        for f in (*c.request, *c.response)
        if f.status is Status.OBSERVED
    ]
    assert not observed, (
        f"{observed} is marked OBSERVED. If a real probe run confirmed it, "
        f"commit the report and update this test. If not, the status is a lie."
    )


def test_the_three_carried_foundry_behaviours_are_all_declared() -> None:
    """They are the reason the probe exists: documentation got all three
    wrong on Foundry and only a live call settled it."""
    carried = {
        f.path for c in CONTRACTS for f in (*c.request, *c.response)
        if f.status is Status.CARRIED
    }
    assert "additionalModelRequestFields.response_format.type" in carried
    assert "output.message.reasoning_content" in carried
    # Behaviour (3), the sentinels, is handled unconditionally rather than
    # being a field — so it lives in the contract's notes, and this asserts
    # it was not simply forgotten.
    assert any("START_TEXT" in note for note in CHAT_CONVERSE.notes)


def test_every_contract_is_serialisable_for_the_report() -> None:
    """diff_report output is written into the probe's JSON artifact."""
    json.dumps(diff_report({}))


# ---------------------------------------------------------------------------
# The diff itself
# ---------------------------------------------------------------------------


def test_a_missing_section_is_not_observed_rather_than_failed() -> None:
    """The distinction the probe already draws, kept here: 'it failed' and
    'it could not run' are different facts."""
    assert diff_section(CHAT_CONVERSE, None)["status"] == "not_observed"
    assert diff_section(CHAT_CONVERSE, {"skipped": "unset"})["status"] == "not_observed"
    assert diff_section(CHAT_CONVERSE, {"error": {"code": "403"}})["status"] == "not_observed"


def test_an_empty_report_confirms_nothing_and_claims_nothing() -> None:
    """A run with no credentials produces exactly this, and it must not read
    as a pass — the defect this migration already shipped once."""
    result = diff_report({})
    assert result["calls_observed"] == []
    assert result["contract_holds"] is False


def test_observed_keys_are_confirmed_against_the_declaration() -> None:
    section = {
        "with_response_format": {
            "content_block_keys": ["text", "reasoningContent"],
            "message_sibling_keys": [],
        }
    }
    result = diff_section(CHAT_CONVERSE, section)
    assert result["status"] == "observed"
    assert "text" in result["confirmed"]
    assert "reasoningContent" in result["confirmed"]


def test_a_required_field_the_probe_looked_for_and_missed_is_called_out() -> None:
    """This is the adapter reading something Bedrock does not send — the
    answer path is broken, not merely unverified, and the diff says which."""
    section = {"only": {"content_block_keys": ["reasoningContent"]}}
    result = diff_section(CHAT_CONVERSE, section)
    assert "text" in result["required_missing"]
    assert diff_report({"chat": section})["contract_holds"] is False


def test_an_undeclared_key_is_surfaced_rather_than_ignored() -> None:
    """The discovery half: a field arriving that no adapter reads."""
    section = {"only": {"content_block_keys": ["text", "citationsContent"]}}
    result = diff_section(CHAT_CONVERSE, section)
    assert result["undeclared"] == ["citationsContent"]


def test_looking_and_seeing_nothing_differs_from_never_looking() -> None:
    """Collapsing these is exactly the kind of thing bedrock_wire exists to
    prevent: one means the probe never got that far, the other means it did
    and the field was absent."""
    never = diff_section(CHAT_CONVERSE, {"only": {"latency_s": 1.0}})
    looked = diff_section(CHAT_CONVERSE, {"only": {"content_block_keys": []}})
    assert never["status"] == "not_observed"
    assert looked["status"] == "observed"
    assert "text" in looked["required_missing"]
    assert never["required_missing"] == []


def test_a_fully_confirmed_chat_section_holds() -> None:
    report = {
        "chat": {
            "v": {
                "content_block_keys": ["text", "reasoningContent"],
                "message_sibling_keys": ["reasoning_content", "reasoning"],
            }
        }
    }
    result = diff_report(report)
    assert result["calls_observed"] == ["chat_converse"]
    assert not result["required_fields_missing"]
    assert result["contract_holds"] is True


# ---------------------------------------------------------------------------
# The probe's use of the diff
# ---------------------------------------------------------------------------


def _probe_module():
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parents[3] / "ops" / "validation"))
    import bedrock_probe

    return bedrock_probe


def test_the_probe_resolves_the_real_contract_not_its_fallback() -> None:
    """The probe bootstraps src/fastapi onto sys.path to import the contract,
    and degrades to a skip if that fails. A silent degrade would mean every
    future run reports "skipped" and nobody notices the diff stopped running
    — the same absence-as-success shape the verdict function exists to stop.
    """
    probe = _probe_module()
    assert probe._DIFF_IMPORT_ERROR is None, (
        f"the probe could not import app.services.bedrock_wire "
        f"({probe._DIFF_IMPORT_ERROR}); every run would silently skip the diff"
    )
    assert "skipped" not in probe._diff_report({})


def test_the_probe_diff_of_an_unauthenticated_run_claims_nothing() -> None:
    """What a credential-less run actually produces."""
    probe = _probe_module()
    empty = {name: {"error": {"code": "InvalidClientTokenId"}}
             for name in ("chat", "chat_stream", "embed", "rerank", "parse")}
    diff = probe._diff_report(empty)
    assert diff["calls_observed"] == []
    assert diff["contract_holds"] is False
    assert not diff["required_fields_missing"]
