"""Unit tests for page-image verbalization (2026-08-18; Bedrock 2026-09-08).

Fully offline — no model call, no object storage. What's pinned here is the
behaviour that costs money or corrupts data when it regresses:

  - Strict opt-in, and no request at all while disabled.
  - Fail-soft: verbalization is additive, so an outage must degrade to
    placeholder text, never raise into the sweep.
  - The anti-transcription prompt, which is the only guard between a VLM and
    an invented ore grade.

Rewritten for ADR-0022. One pinned behaviour did not survive the move and is
called out where it used to be asserted: Foundry's `detail: low` request knob
bounded per-page token cost, and Bedrock Converse has no equivalent, so
`image_detail()` now selects the prompt only.

The other change worth knowing: there is no default model any more. Foundry's
`gpt-5-mini` has no Bedrock counterpart, and this capability was never a
Cohere model, so "keep the model, change the host" does not apply. The module
reports itself unconfigured until one is chosen.
"""

from __future__ import annotations

import pytest

from app.services import _bedrock
from app.services.ingest import page_vision_client as vision

MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        vision.ENABLED_ENV, vision.MODEL_ENV, vision.MODEL_ID_ENV,
        "IMAGE_VERBALIZATION_DETAIL",
    ):
        monkeypatch.delenv(var, raising=False)
    _bedrock.reset_client_cache()


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(vision.ENABLED_ENV, "true")
    monkeypatch.setenv(vision.MODEL_ID_ENV, MODEL_ID)


def _install(monkeypatch: pytest.MonkeyPatch, handler) -> dict:
    """Replace the Bedrock client with a fake; return the captured call."""
    seen: dict = {}

    class _Client:
        @staticmethod
        def converse(**kwargs):
            seen.update(kwargs)
            return handler(**kwargs)

    monkeypatch.setattr(
        _bedrock, "get_client", lambda service, **_kw: _Client()
    )
    return seen


def _ok(text: str = "A geological cross-section."):
    def handler(**_kwargs):
        return {"output": {"message": {"content": [{"text": text}]}}}

    return handler


class TestOptIn:
    def test_disabled_by_default(self) -> None:
        assert vision.is_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_recognised_truthy_values(
        self, value: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(vision.ENABLED_ENV, value)
        assert vision.is_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe"])
    def test_anything_else_stays_off(
        self, value: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(vision.ENABLED_ENV, value)
        assert vision.is_enabled() is False

    def test_disabled_short_circuits_before_any_network_call(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def explode(*_a, **_kw):  # pragma: no cover - must not run
            raise AssertionError("made a request while disabled")

        monkeypatch.setattr(_bedrock, "get_client", explode)
        out = vision.verbalize_page(b"\x89PNG")
        assert out.ok is False
        assert out.error == "disabled"

    def test_enabled_without_a_model_fails_softly(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """There is deliberately no default model — see the module docstring.

        Guessing a Bedrock vision model would change what every image
        passage says with no eval behind it, so an unset value has to be an
        error rather than a fallback.
        """
        monkeypatch.setenv(vision.ENABLED_ENV, "true")
        out = vision.verbalize_page(b"\x89PNG")
        assert out.ok is False
        assert vision.MODEL_ID_ENV in (out.error or "")


class TestPrompt:
    def test_forbids_transcribing_numbers_out_of_tables(self) -> None:
        """The one guard between a VLM and an invented ore grade."""
        prompt = vision.PROMPT.lower()
        assert "do not transcribe numeric values" in prompt
        assert "assay results, grades or tonnages" in prompt

    def test_asks_for_the_entities_that_make_a_figure_findable(self) -> None:
        """At HIGH detail, where the model is sent enough pixels to read them.

        This used to assert the same tokens against the default prompt,
        which ships at detail="low" -- a single downsampled tile. Asking a
        model to name drill holes and formations it cannot see does not
        get a refusal, it gets plausible invented ones, and they become
        silver.document_passages.text.
        """
        prompt = vision.build_prompt("high").lower()
        for token in ("cross-section", "drill-hole", "formation", "caption"):
            assert token in prompt

    def test_the_low_detail_prompt_asks_for_none_of_them(self) -> None:
        """The default, and what the live worker sends."""
        prompt = vision.build_prompt("low").lower()

        assert "cross-section" in prompt, (
            "figure KIND survives downsampling and is what makes the "
            "description useful at all"
        )
        for token in ("quoted exactly", "named entities"):
            assert token not in prompt, token

    def test_the_default_prompt_is_the_low_detail_one(self) -> None:
        """`PROMPT` is the alias other modules and docstrings reference;
        it must track the detail the requests actually use."""
        assert vision.build_prompt("low") == vision.PROMPT

    def test_forbids_inference_beyond_the_page(self) -> None:
        assert "do not infer" in vision.PROMPT.lower()


class TestRequestShape:
    def test_targets_the_confirmed_foundry_openai_v1_path(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable(monkeypatch)
        seen = _install(monkeypatch, _ok())
        vision.verbalize_page(b"\x89PNG")

        assert seen["modelId"] == MODEL_ID
        # No endpoint and no key: the task role signs the request.

    def test_sends_the_image_as_bytes_alongside_the_prompt(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable(monkeypatch)
        seen = _install(monkeypatch, _ok())
        vision.verbalize_page(b"\x89PNG")

        content = seen["messages"][0]["content"]
        assert any("text" in part for part in content)

        image = next(p for p in content if "image" in p)["image"]
        assert image["format"] == "png"
        # Converse takes raw bytes — no base64 data URI round trip.
        assert image["source"]["bytes"] == b"\x89PNG"

    def test_output_tokens_are_capped(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bedrock has no `detail` knob, so maxTokens is the whole of what
        bounds per-page cost now. At IMAGE_EMBED_PAGE_SCOPE=all that
        multiplies across every page of every document, which is why the
        Foundry version pinned `detail: low` here."""
        _enable(monkeypatch)
        seen = _install(monkeypatch, _ok())
        vision.verbalize_page(b"\x89PNG")

        assert seen["inferenceConfig"]["maxTokens"] == vision._MAX_TOKENS

    def test_the_model_is_overridable(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MODEL_ENV keeps working so an existing override survives the move,
        but it now carries a Bedrock model id."""
        _enable(monkeypatch)
        seen = _install(monkeypatch, _ok())

        vision.verbalize_page(b"\x89PNG")
        assert seen["modelId"] == MODEL_ID

        monkeypatch.setenv(vision.MODEL_ENV, "amazon.nova-lite-v1:0")
        vision.verbalize_page(b"\x89PNG")
        assert seen["modelId"] == "amazon.nova-lite-v1:0"


class TestResponseParsing:
    def test_parses_a_single_text_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install(monkeypatch, _ok("A plan view."))
        out = vision.verbalize_page(b"\x89PNG")
        assert out.ok is True
        assert out.text == "A plan view."

    def test_joins_multiple_text_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install(
            monkeypatch,
            lambda **_kw: {
                "output": {
                    "message": {
                        "content": [{"text": "Long section."}, {"text": "Scale 1:500."}]
                    }
                }
            },
        )
        assert vision.verbalize_page(b"\x89PNG").text == "Long section.\nScale 1:500."

    def test_non_text_blocks_are_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A reasoning block is not the page description and must not be
        stringified into one."""
        _enable(monkeypatch)
        _install(
            monkeypatch,
            lambda **_kw: {
                "output": {
                    "message": {
                        "content": [
                            {"reasoningContent": {"text": "thinking..."}},
                            {"text": "A cross-section."},
                        ]
                    }
                }
            },
        )
        assert vision.verbalize_page(b"\x89PNG").text == "A cross-section."

    def test_a_malformed_response_degrades_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable(monkeypatch)
        _install(monkeypatch, lambda **_kw: {"output": {}})
        out = vision.verbalize_page(b"\x89PNG")
        assert out.ok is False
        assert "unparseable_response" in (out.error or "")

    def test_empty_completion_is_a_failure_not_an_empty_passage(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty description would overwrite the placeholder with nothing."""
        _enable(monkeypatch)
        _install(monkeypatch, _ok("   "))
        out = vision.verbalize_page(b"\x89PNG")
        assert out.ok is False
        assert out.error == "empty_description"


class TestFailSoft:
    def test_transport_error_never_propagates(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable(monkeypatch)

        def boom(**_kw):
            raise ConnectionError("bedrock unreachable")

        _install(monkeypatch, boom)
        out = vision.verbalize_page(b"\x89PNG")
        assert out.ok is False
        assert "ConnectionError" in (out.error or "")

    def test_a_service_error_never_propagates(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from botocore.exceptions import ClientError

        _enable(monkeypatch)

        def denied(**_kw):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                "Converse",
            )

        _install(monkeypatch, denied)
        assert vision.verbalize_page(b"\x89PNG").ok is False

    def test_client_construction_failure_never_propagates(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Building the client is inside the try for a reason: a bad region
        or missing credentials raises there, not at call time."""
        _enable(monkeypatch)

        def boom(*_a, **_kw):
            raise RuntimeError("no credentials")

        monkeypatch.setattr(_bedrock, "get_client", boom)
        out = vision.verbalize_page(b"\x89PNG")
        assert out.ok is False
        assert "no credentials" in (out.error or "")

class TestThePromptMatchesTheResolution:
    """The prompt asked for exact quotes from an image sent at low detail.

    `verbalize_page` sends the page with `detail` defaulting to "low" — a
    single downsampled tile — while the prompt asked for the figure's
    "title and caption, quoted exactly" and for drill-hole IDs, grid and
    scale.

    A model asked to quote exactly from an image it cannot read does not
    refuse. It writes plausible text: correctly-formatted hole IDs that
    appear on no sheet. That text becomes
    `silver.document_passages.text`, is patched into the Qdrant payload,
    and reaches the reranker and the answer path.

    The prompt is now derived from the detail level so the two cannot
    contradict each other again.
    """

    def test_low_detail_does_not_ask_for_exact_quotes(self) -> None:
        from app.services.ingest.page_vision_client import build_prompt

        prompt = build_prompt("low")

        assert "quoted exactly" not in prompt
        assert "drill-hole \nIDs" not in prompt

    def test_low_detail_asks_the_model_to_admit_illegibility(self) -> None:
        """The replacement instruction has to be positive, not merely an
        omission. Left to itself a model still guesses at a label."""
        from app.services.ingest.page_vision_client import build_prompt

        prompt = build_prompt("low")

        assert "not clearly readable" in prompt
        assert "invents a plausible label" in prompt

    def test_high_detail_asks_for_them_again(self) -> None:
        """At high detail the model is actually sent the pixels, so the
        instruction is answerable and the extra tokens buy something."""
        from app.services.ingest.page_vision_client import build_prompt

        prompt = build_prompt("high")

        assert "quoted exactly" in prompt
        assert "Named entities" in prompt

    def test_the_never_invent_rule_applies_at_both_levels(self) -> None:
        from app.services.ingest.page_vision_client import build_prompt

        for level in ("low", "high"):
            assert "invents a plausible label" in build_prompt(level), level

    def test_an_unknown_detail_value_gets_the_cautious_prompt(self) -> None:
        """A typo in IMAGE_VERBALIZATION_DETAIL must not silently select
        the instruction set the resolution cannot support."""
        from app.services.ingest.page_vision_client import build_prompt

        assert "quoted exactly" not in build_prompt("hgih")
        assert "quoted exactly" not in build_prompt("")

    def test_the_default_detail_is_low(self, monkeypatch) -> None:
        from app.services.ingest.page_vision_client import image_detail

        monkeypatch.delenv("IMAGE_VERBALIZATION_DETAIL", raising=False)
        assert image_detail() == "low"

    def test_the_env_var_selects_the_prompt_too(self, monkeypatch) -> None:
        """The point of the derivation: raising the detail raises what is
        asked for, in one edit rather than two."""
        from app.services.ingest.page_vision_client import (
            build_prompt,
            image_detail,
        )

        monkeypatch.setenv("IMAGE_VERBALIZATION_DETAIL", "HIGH")
        assert image_detail() == "high"
        assert "quoted exactly" in build_prompt()

    def test_the_prompt_is_built_from_one_resolved_value(self) -> None:
        """Reading the environment twice let the prompt and the image
        describe different resolutions within one request.

        Half of that hazard is gone: Bedrock Converse has no `detail` knob,
        so the model always gets the full image and image_detail() selects
        the prompt only (ADR-0022). The single-read discipline stays because
        build_prompt still branches on it, and a prompt that asks for exact
        quotes is the difference between a description and an invented
        transcription.
        """
        import inspect

        from app.services.ingest import page_vision_client

        source = inspect.getsource(page_vision_client.verbalize_page)

        assert "_detail = image_detail()" in source
        assert "build_prompt(_detail)" in source
        assert source.count("= image_detail()") == 1, "resolve it once, use it twice"
