"""Unit tests for Foundry page-image verbalization (2026-08-18).

Fully offline — no Foundry call, no object storage. What's pinned here is the
behaviour that costs money or corrupts data when it regresses:

  - Strict opt-in, and no request at all while disabled.
  - Fail-soft: verbalization is additive, so an outage must degrade to
    placeholder text, never raise into the sweep.
  - The anti-transcription prompt, which is the only guard between a VLM and
    an invented ore grade.
  - `detail: low`, which at scope=all is the difference between a sane bill
    and a large one.
"""

from __future__ import annotations

import pytest

from app.services.ingest import page_vision_client as vision


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        vision.ENABLED_ENV, vision.MODEL_ENV,
        vision.ENDPOINT_ENV, vision.KEY_ENV,
        "IMAGE_VERBALIZATION_DETAIL",
    ):
        monkeypatch.delenv(var, raising=False)


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(vision.ENABLED_ENV, "true")
    monkeypatch.setenv(vision.ENDPOINT_ENV, "https://foundry.invalid")
    monkeypatch.setenv(vision.KEY_ENV, "test-key")


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


def _ok(text: str = "A geological cross-section.") -> _FakeResponse:
    return _FakeResponse({"choices": [{"message": {"content": text}}]})


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

        monkeypatch.setattr("httpx.post", explode)
        out = vision.verbalize_page(b"\x89PNG")
        assert out.ok is False
        assert out.error == "disabled"

    def test_enabled_without_credentials_fails_softly(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(vision.ENABLED_ENV, "true")
        out = vision.verbalize_page(b"\x89PNG")
        assert out.ok is False
        assert vision.ENDPOINT_ENV in (out.error or "")


class TestPrompt:
    def test_forbids_transcribing_numbers_out_of_tables(self) -> None:
        """The one guard between a VLM and an invented ore grade."""
        prompt = vision.PROMPT.lower()
        assert "do not transcribe numeric values" in prompt
        assert "assay results, grades or tonnages" in prompt

    def test_asks_for_the_entities_that_make_a_figure_findable(self) -> None:
        prompt = vision.PROMPT.lower()
        for token in ("cross-section", "drill-hole", "formation", "caption"):
            assert token in prompt

    def test_forbids_inference_beyond_the_page(self) -> None:
        assert "do not infer" in vision.PROMPT.lower()


class TestRequestShape:
    def test_targets_the_confirmed_foundry_openai_v1_path(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable(monkeypatch)
        seen: dict = {}

        def capture(url, **kw):
            seen["url"] = url
            seen["headers"] = kw.get("headers")
            seen["json"] = kw.get("json")
            return _ok()

        monkeypatch.setattr("httpx.post", capture)
        vision.verbalize_page(b"\x89PNG")

        assert seen["url"] == "https://foundry.invalid/openai/v1/chat/completions"
        # Foundry authenticates with api-key, not a bearer token.
        assert seen["headers"]["api-key"] == "test-key"

    def test_sends_the_image_as_a_data_uri_alongside_the_prompt(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable(monkeypatch)
        seen: dict = {}
        monkeypatch.setattr(
            "httpx.post",
            lambda url, **kw: (seen.update(kw.get("json") or {}), _ok())[1],
        )
        vision.verbalize_page(b"\x89PNG")

        content = seen["messages"][0]["content"]
        kinds = [part["type"] for part in content]
        assert "text" in kinds and "image_url" in kinds

        image = next(p for p in content if p["type"] == "image_url")
        assert image["image_url"]["url"].startswith("data:image/png;base64,")

    def test_detail_defaults_to_low_to_bound_token_cost(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """detail=high tiles each page into many more tokens.

        At IMAGE_EMBED_PAGE_SCOPE=all that multiplies across every page of
        every document, so the default matters more than it looks.
        """
        _enable(monkeypatch)
        seen: dict = {}
        monkeypatch.setattr(
            "httpx.post",
            lambda url, **kw: (seen.update(kw.get("json") or {}), _ok())[1],
        )
        vision.verbalize_page(b"\x89PNG")

        image = next(
            p for p in seen["messages"][0]["content"] if p["type"] == "image_url"
        )
        assert image["image_url"]["detail"] == "low"

    def test_model_defaults_to_the_chosen_one_and_is_overridable(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable(monkeypatch)
        seen: dict = {}
        monkeypatch.setattr(
            "httpx.post",
            lambda url, **kw: (seen.update(kw.get("json") or {}), _ok())[1],
        )

        vision.verbalize_page(b"\x89PNG")
        assert seen["model"] == "gpt-5-mini"

        monkeypatch.setenv(vision.MODEL_ENV, "gpt-4o")
        vision.verbalize_page(b"\x89PNG")
        assert seen["model"] == "gpt-4o"


class TestResponseParsing:
    def test_parses_a_plain_string_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        monkeypatch.setattr("httpx.post", lambda *a, **kw: _ok("A plan view."))
        out = vision.verbalize_page(b"\x89PNG")
        assert out.ok is True
        assert out.text == "A plan view."

    def test_parses_typed_content_parts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        monkeypatch.setattr(
            "httpx.post",
            lambda *a, **kw: _FakeResponse({
                "choices": [
                    {"message": {"content": [{"type": "text", "text": "Long section."}]}}
                ]
            }),
        )
        assert vision.verbalize_page(b"\x89PNG").text == "Long section."

    def test_no_choices_degrades_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable(monkeypatch)
        monkeypatch.setattr("httpx.post", lambda *a, **kw: _FakeResponse({"choices": []}))
        out = vision.verbalize_page(b"\x89PNG")
        assert out.ok is False
        assert "unparseable_response" in (out.error or "")

    def test_empty_completion_is_a_failure_not_an_empty_passage(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty description would overwrite the placeholder with nothing."""
        _enable(monkeypatch)
        monkeypatch.setattr("httpx.post", lambda *a, **kw: _ok("   "))
        out = vision.verbalize_page(b"\x89PNG")
        assert out.ok is False
        assert out.error == "empty_description"


class TestFailSoft:
    def test_transport_error_never_propagates(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable(monkeypatch)

        def boom(*_a, **_kw):
            raise ConnectionError("foundry unreachable")

        monkeypatch.setattr("httpx.post", boom)
        out = vision.verbalize_page(b"\x89PNG")
        assert out.ok is False
        assert "ConnectionError" in (out.error or "")

    def test_http_error_never_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        monkeypatch.setattr("httpx.post", lambda *a, **kw: _FakeResponse({}, status=500))
        assert vision.verbalize_page(b"\x89PNG").ok is False
