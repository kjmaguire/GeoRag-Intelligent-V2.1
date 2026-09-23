"""Cohere Parse adapter — wire shape, response adaptation, failure modes.

No network: the single seam is ``cohere_parse_client._invoke``, replaced with
a fake that returns hand-rolled response bodies. Rendering is replaced too, so
these tests do not need a PDF; the render path has its own tests in
test_cohere_parse_pixel_cap.

Rewritten twice for a transport move, and the point of the file both times
is that the transport is ALL that moved. 2026-09-08 (ADR-0022) took it from
Foundry to Bedrock; 2026-09-15 (ADR-0023) takes it from Bedrock to Cohere's
own API. The seam kept its shape across both — ``_invoke(model, body) ->
bytes`` — and the response adapter has never been touched, which is the
claim these tests exist to keep checkable.

What changed in the ADR-0023 pass:

- ``model`` is back IN the request body. Bedrock had moved it out to
  ``modelId``; Cohere's own API takes it inline, which is the shape
  ADR-0019 first wrote against.
- Failures are HTTP statuses, not botocore error codes. 401/403/404 are
  operator problems and log at ERROR; 429 and 5xx are weather.
- The retry ladder is BACK. botocore used to own it, so a throttle reaching
  this adapter had already been retried and the only question left was
  whether the fallback was clean. httpx retries nothing, so the retries are
  explicit in ``_invoke`` and tested here — without them the move to this
  host would quietly push more pages onto Tesseract.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.services.ingest import cohere_parse_client as cpc

FIXTURES = Path(__file__).parent / "fixtures" / "cohere_parse"

API_KEY = "test-only-not-a-real-cohere-key"
MODEL = "parse-v5.0"


def _body(payload) -> bytes:
    """A successful Parse response body."""
    return json.dumps(payload).encode()


def _http_error(status: int, message: str = "") -> cpc.CohereParseHttpError:
    return cpc.CohereParseHttpError(status, message)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("OCR_ENGINE", "cohere_parse")
    monkeypatch.setenv("COHERE_API_KEY", API_KEY)
    monkeypatch.delenv("COHERE_PARSE_MODEL", raising=False)
    monkeypatch.delenv("COHERE_BASE_URL", raising=False)
    # ADR-0023 retired this for OCR; left set it only produces a warning,
    # but an unrelated one in the middle of a caplog assertion is noise.
    monkeypatch.delenv("BEDROCK_PARSE_MODEL_ID", raising=False)
    # assert_no_retired_foundry_env reads the live environment, so a
    # developer's own Azure credentials would otherwise fail every test here.
    for name in (
        "AZURE_FOUNDRY_ENDPOINT",
        "AZURE_FOUNDRY_API_KEY",
        "AZURE_FOUNDRY_PARSE_DEPLOYMENT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("COHERE_PARSE_OUTPUT_FORMAT", raising=False)
    monkeypatch.delenv("COHERE_PARSE_INCLUDE_IMAGE_DESCRIPTIONS", raising=False)
    # Rendering is not under test here.
    monkeypatch.setattr(cpc, "_page_count", lambda _path: 999)
    monkeypatch.setattr(cpc, "_render_page", lambda _path, page: b"\x89PNG-fake-" + str(page).encode())


@pytest.fixture(params=["blocks_page.json", "blocks_page_sdk.json"], ids=["flat", "sdk_nested"])
def blocks_payload(request):
    """Both block spellings: fields on the block (this adapter's first
    guess) and nested under ``block[type]`` (the Cohere SDK's types). Same
    page content, so every assertion holds for either."""
    return json.loads((FIXTURES / request.param).read_text())


@pytest.fixture
def markdown_payload():
    return json.loads((FIXTURES / "markdown_page.json").read_text())


def _capture_invoke(monkeypatch, responses):
    """Replace the network seam; return the list of captured calls.

    Each entry in ``responses`` is either bytes (a response body) or an
    exception to raise. The last entry repeats once the queue is down to one,
    which keeps the multi-page tests from needing one entry per page.
    """
    calls: list[dict] = []
    queue = list(responses)

    def fake_invoke(model, body):
        calls.append({"model": model, "body": body})
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(cpc, "_invoke", fake_invoke)
    return calls


class TestSelectionAndConfiguration:
    def test_engine_is_selected_by_ocr_engine_value(self, monkeypatch) -> None:
        assert cpc.is_engine_selected()
        monkeypatch.setenv("OCR_ENGINE", "tesseract")
        assert not cpc.is_engine_selected()

    def test_is_configured_needs_the_api_key(self, monkeypatch) -> None:
        """The credential IS the check now.

        Under Bedrock it deliberately was not — the task role supplied it and
        there was nothing in the environment to read. On this host a missing
        key is visible before a page is even rendered, so it is worth
        catching up front rather than as a 401 per page.
        """
        assert cpc.is_configured()
        monkeypatch.delenv("COHERE_API_KEY")
        assert not cpc.is_configured()

    def test_a_blank_key_is_not_configured(self, monkeypatch) -> None:
        """The shape a half-filled secret leaves behind."""
        monkeypatch.setenv("COHERE_API_KEY", "   ")
        assert not cpc.is_configured()

    def test_the_model_name_defaults_and_is_overridable(self, monkeypatch) -> None:
        assert cpc.parse_model() == MODEL
        monkeypatch.setenv("COHERE_PARSE_MODEL", "parse-v6.0")
        assert cpc.parse_model() == "parse-v6.0"

    def test_the_base_url_defaults_and_loses_a_trailing_slash(self, monkeypatch) -> None:
        assert cpc.base_url() == "https://api.cohere.com"
        monkeypatch.setenv("COHERE_BASE_URL", "https://proxy.internal/cohere/")
        assert cpc.base_url() == "https://proxy.internal/cohere"

    def test_a_leftover_bedrock_model_id_is_warned_about_not_obeyed(self, monkeypatch, caplog, blocks_payload) -> None:
        """Not a raise, unlike the Foundry variables.

        BEDROCK_PARSE_MODEL_ID may legitimately still be set: ADR-0023 took
        Bedrock's default, not its support, and an operator running the
        Marketplace endpoint for chat could want it. It is still worth
        saying, because OCR is no longer billed through it and nothing else
        would reveal that.
        """
        monkeypatch.setenv("BEDROCK_PARSE_MODEL_ID", "arn:aws:sagemaker:...")
        _capture_invoke(monkeypatch, [_body(blocks_payload)])

        with caplog.at_level(logging.WARNING, logger="georag.ingest.cohere_parse"):
            result = cpc.ocr_page_sync("/x.pdf", 1)

        assert result.request_succeeded, "the leftover must not break OCR"
        assert any("BEDROCK_PARSE_MODEL_ID" in r.getMessage() for r in caplog.records)

    def test_missing_config_raises_not_configured_at_call_time(self, monkeypatch) -> None:
        monkeypatch.delenv("COHERE_API_KEY")

        with pytest.raises(cpc.CohereParseNotConfigured):
            cpc.ocr_page_sync("/x.pdf", 1)
        with pytest.raises(cpc.CohereParseNotConfigured):
            cpc.ocr_page_block_sync("/x.pdf", [1, 2])

    def test_leftover_foundry_config_is_rejected(self, monkeypatch) -> None:
        """A half-migrated worker must stop, not quietly OCR with tesseract.

        This is the 2026-08-21 failure in its new clothes: an environment
        that still names the retired host is well-formed, so nothing would
        otherwise notice, and every scanned page would lose its tables.
        """
        from app.services._bedrock import RetiredAzureConfiguration

        monkeypatch.setenv("AZURE_FOUNDRY_PARSE_DEPLOYMENT", "Cohere-parse-v5")

        with pytest.raises(RetiredAzureConfiguration, match="BEDROCK"):
            cpc.ocr_page_sync("/x.pdf", 1)

    def test_pages_per_batch_reads_and_clamps(self, monkeypatch) -> None:
        monkeypatch.delenv("OCR_PAGES_PER_BATCH", raising=False)
        assert cpc.pages_per_batch() == 8
        monkeypatch.setenv("OCR_PAGES_PER_BATCH", "5000")
        assert cpc.pages_per_batch() == 32
        monkeypatch.setenv("OCR_PAGES_PER_BATCH", "0")
        assert cpc.pages_per_batch() == 1
        monkeypatch.setenv("OCR_PAGES_PER_BATCH", "eight")
        assert cpc.pages_per_batch() == 8


class TestWireShape:
    def test_request_carries_the_model_and_a_data_uri(self, monkeypatch, blocks_payload) -> None:
        """The body is Cohere's own parse body again.

        This assertion is the migration's central claim about this adapter:
        the document half of the request never changed across three hosts, so
        the response adapter below never had to be touched either. What moved
        is where ``model`` lives — Foundry and Cohere take it inline, Bedrock
        took it out to ``modelId``.
        """
        calls = _capture_invoke(monkeypatch, [_body(blocks_payload)])

        cpc.ocr_page_sync("/x.pdf", 3)

        assert calls[0]["model"] == MODEL
        body = calls[0]["body"]
        assert set(body) == {"document", "output_format"}
        assert body["document"]["type"] == "image_url"
        # A bare string: Cohere 400s the chat-style {"url": ...} object
        # ("parameter 'document.image_url' is of type object but should be of
        # type string" — the first live call, 2026-09-23).
        assert isinstance(body["document"]["image_url"], str)
        assert body["document"]["image_url"].startswith("data:image/png;base64,")
        assert body["output_format"] == "blocks"

    def test_output_format_env_reaches_the_body_and_invalid_falls_back(
        self, monkeypatch, blocks_payload, caplog
    ) -> None:
        calls = _capture_invoke(monkeypatch, [_body(blocks_payload)])
        monkeypatch.setenv("COHERE_PARSE_OUTPUT_FORMAT", "markdown")
        cpc.ocr_page_sync("/x.pdf", 1)
        assert calls[-1]["body"]["output_format"] == "markdown"

        monkeypatch.setenv("COHERE_PARSE_OUTPUT_FORMAT", "yaml")
        with caplog.at_level(logging.WARNING, logger="georag.ingest.cohere_parse"):
            cpc.ocr_page_sync("/x.pdf", 1)
        assert calls[-1]["body"]["output_format"] == "blocks"
        assert any("COHERE_PARSE_OUTPUT_FORMAT" in r.getMessage() for r in caplog.records)


class TestResponseAdapter:
    def test_blocks_become_text_in_order_with_a_table_grid(self, monkeypatch, blocks_payload) -> None:
        _capture_invoke(monkeypatch, [_body(blocks_payload)])

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert result.request_succeeded
        assert result.confidence_reported is False
        assert result.words == ()
        assert result.mean_confidence == 0.0
        assert result.detected_region_count == 0
        assert result.text.startswith("# 14 MINERAL RESOURCE ESTIMATES")
        assert result.text.rstrip().endswith("Mineral resources are not mineral reserves.")
        # The table is both a grid (for the table sections) and inline text.
        assert len(result.tables) == 1
        grid = result.tables[0]
        assert grid[0] == ["Category", "Tonnes (Mt)", "Grade", "Grade"]
        assert grid[1] == ["Category", "Tonnes (Mt)", "Au (g/t)", "Ag (g/t)"]
        assert grid[4] == ["Inferred", "0.8", "1.1", "7.5"]
        assert "Inferred" in result.text and "7.5" in result.text
        # Image descriptions stay out by default.
        assert "Plan view map" not in result.text

    def test_a_nested_text_object_is_read_not_stringified(self, monkeypatch) -> None:
        """The SDK's text block is ``{"text": {"content": ...}}``. Reading
        ``text`` with a take-whatever-is-there helper put the dict's repr
        into the page text — ``{'content': 'DDH-24-001'}`` — which chunks,
        embeds and cites like real text while being corrupt."""
        _capture_invoke(
            monkeypatch,
            [
                _body(
                    {
                        "pages": [
                            {
                                "type": "blocks",
                                "index": 0,
                                "blocks": [{"type": "text", "text": {"content": "DDH-24-001"}}],
                            }
                        ]
                    }
                )
            ],
        )

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert result.request_succeeded
        assert result.text == "DDH-24-001"

    def test_markdown_page_in_the_sdk_shape(self, monkeypatch) -> None:
        _capture_invoke(
            monkeypatch,
            [_body({"pages": [{"type": "markdown", "index": 0, "markdown": {"content": "# Collars", "images": []}}]})],
        )

        assert cpc.ocr_page_sync("/x.pdf", 1).text == "# Collars"

    def test_image_descriptions_are_opt_in(self, monkeypatch, blocks_payload) -> None:
        _capture_invoke(monkeypatch, [_body(blocks_payload)])
        monkeypatch.setenv("COHERE_PARSE_INCLUDE_IMAGE_DESCRIPTIONS", "1")

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert "[Figure: Plan view map of the Madison deposit showing drill collars.]" in result.text

    def test_markdown_mode_strips_image_refs_and_converts_html_tables(self, monkeypatch, markdown_payload) -> None:
        _capture_invoke(monkeypatch, [_body(markdown_payload)])

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert result.request_succeeded
        assert "![" not in result.text
        assert "<table>" not in result.text
        assert "img_1" not in result.text
        assert len(result.tables) == 1
        assert result.tables[0][0] == ["Category", "Tonnes (Mt)", "Au (g/t)"]
        assert "Indicated" in result.text

    def test_markdown_as_a_plain_string_is_accepted(self, monkeypatch) -> None:
        _capture_invoke(monkeypatch, [_body({"pages": [{"markdown": "Just prose."}]})])

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert result.text == "Just prose."
        assert result.tables == []

    def test_an_empty_page_is_a_success_with_no_text(self, monkeypatch) -> None:
        _capture_invoke(monkeypatch, [_body({"pages": [{"blocks": []}]})])

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert result.request_succeeded
        assert result.text == ""

    def test_page_text_is_stripped_so_joiner_arithmetic_stays_exact(self, monkeypatch) -> None:
        _capture_invoke(
            monkeypatch,
            [_body({"pages": [{"blocks": [{"type": "text", "text": "  hello \n\n"}]}]})],
        )

        assert cpc.ocr_page_sync("/x.pdf", 1).text == "hello"


class TestFailureModes:
    def test_a_rejected_request_fails_soft_with_the_status(self, monkeypatch) -> None:
        _capture_invoke(monkeypatch, [_http_error(413, "image too large")])

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert not result.request_succeeded
        assert result.error.startswith("http_413:")
        assert "image too large" in result.error
        assert result.confidence_reported is False

    @pytest.mark.parametrize("status", [401, 403, 404])
    def test_a_refused_call_is_logged_at_error(self, monkeypatch, caplog, status) -> None:
        """This branch has earned its level on every host.

        On Foundry it was an HTTP 403, and Foundry blocked 1,421 of 2,524
        calls on 2026-08-17 with nothing noticing. On Bedrock the same
        condition at least also showed up in InvocationClientErrors, which
        was alarmed. On Cohere's API there is no AWS metric behind it at all
        — CloudWatch cannot see a call that never went to AWS — so this log
        line is the whole signal.
        """
        _capture_invoke(monkeypatch, [_http_error(status, "nope")])

        with caplog.at_level(logging.ERROR, logger="georag.ingest.cohere_parse"):
            result = cpc.ocr_page_sync("/x.pdf", 1)

        assert not result.request_succeeded
        assert any(r.levelno == logging.ERROR and "COHERE_PARSE_REJECTED" in r.getMessage() for r in caplog.records), (
            "the alarm marker is what pages on this; without it nothing does"
        )

    def test_the_refusal_log_names_the_variable_to_check(self, monkeypatch, caplog) -> None:
        """An operator reading this line at 3am should not have to guess."""
        _capture_invoke(monkeypatch, [_http_error(401, "invalid api token")])

        with caplog.at_level(logging.ERROR, logger="georag.ingest.cohere_parse"):
            cpc.ocr_page_sync("/x.pdf", 1)

        assert any("COHERE_API_KEY" in r.getMessage() for r in caplog.records)

    def test_an_exhausted_throttle_fails_soft_at_warning(self, monkeypatch, caplog) -> None:
        """A 429 that reaches this adapter has already been retried by
        ``_invoke``, so it is real — but it is weather, not an operator
        error, so it does not get the ERROR level a refusal does."""
        _capture_invoke(monkeypatch, [_http_error(429, "slow down")])

        with caplog.at_level(logging.DEBUG, logger="georag.ingest.cohere_parse"):
            result = cpc.ocr_page_sync("/x.pdf", 1)

        assert not result.request_succeeded
        assert result.error.startswith("http_429:")
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_transport_error_fails_soft(self, monkeypatch) -> None:
        def boom(model, body):
            raise OSError("down")

        monkeypatch.setattr(cpc, "_invoke", boom)

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert not result.request_succeeded
        assert "down" in result.error

    def test_non_json_body_fails_soft(self, monkeypatch) -> None:
        """Kept distinct from a transport failure on purpose: "the API
        refused" and "the API answered with something that is not JSON" want
        different operator responses."""
        _capture_invoke(monkeypatch, [b"<html>gateway</html>"])

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert not result.request_succeeded
        assert result.error.startswith("non_json_response")

    @pytest.mark.parametrize(
        "payload",
        [
            {"result": {"content": "surprise"}},  # plausible alternative shape
            {"pages": [{"unexpected": 1}]},  # page dict, no blocks/markdown
            {"pages": "not-a-list"},
            [],  # top-level array
        ],
    )
    def test_an_unrecognised_shape_fails_soft_and_is_logged(self, monkeypatch, caplog, payload) -> None:
        """HTTP 200 with a body we do not recognise is NOT success.

        Until 2026-09-15 this returned PageOcrResult("", 0.0) with
        request_succeeded left at its True default and no log line at all.
        pdf_report.py:2665 drops to tesseract only `if not
        request_succeeded` — "NOT merely empty text", as its comment says —
        so the page was billed, produced no text and no tables, did not fall
        back, and said nothing. Indistinguishable from a blank sheet.

        This is the failure mode this deployment is most likely to hit:
        Cohere Parse's wire shape has never been verified on any host
        (ADR-0022), so a body we cannot read is exactly what being wrong
        about it looks like.
        """
        _capture_invoke(monkeypatch, [_body(payload)])

        with caplog.at_level(logging.ERROR, logger="georag.ingest.cohere_parse"):
            result = cpc.ocr_page_sync("/x.pdf", 1)

        assert not result.request_succeeded
        assert result.error == "unrecognised_response_shape"
        assert result.text == ""
        assert any(
            r.levelno == logging.ERROR and "COHERE_PARSE_UNRECOGNISED_RESPONSE" in r.getMessage()
            for r in caplog.records
        ), "the marker alerts.tf filters on must appear in the message"

    def test_unrecognised_shape_log_does_not_leak_document_text(self, monkeypatch, caplog) -> None:
        """A Parse body carries the document's text. The diagnostic names the
        top-level keys so the adapter can be corrected; it must not copy
        values into CloudWatch."""
        secret = "CONFIDENTIAL assay 12.4 g/t Au"
        _capture_invoke(monkeypatch, [_body({"unknown_key": secret})])

        with caplog.at_level(logging.ERROR, logger="georag.ingest.cohere_parse"):
            cpc.ocr_page_sync("/x.pdf", 1)

        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "unknown_key" in joined
        assert secret not in joined

    def test_render_failure_fails_soft_without_a_request(self, monkeypatch) -> None:
        calls = _capture_invoke(monkeypatch, [_body({"pages": []})])
        monkeypatch.setattr(cpc, "_render_page", lambda _path, page: None)

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert not result.request_succeeded
        assert result.error == "render_failed"
        assert calls == []


class TestPageGroups:
    def test_group_posts_one_request_per_page_keyed_by_absolute_page(self, monkeypatch, blocks_payload) -> None:
        calls = _capture_invoke(monkeypatch, [_body(blocks_payload)])

        mapping = cpc.ocr_page_block_sync("/x.pdf", [7, 3, 3, 12])

        assert sorted(mapping) == [3, 7, 12]
        assert len(calls) == 3
        assert all(r.request_succeeded for r in mapping.values())

    def test_a_failed_page_is_absent_and_an_empty_page_is_present(self, monkeypatch) -> None:
        by_page = {
            1: _body({"pages": [{"blocks": [{"type": "text", "text": "one"}]}]}),
            2: _http_error(422, "bad"),
            3: _body({"pages": [{"blocks": []}]}),
        }

        def fake_invoke(model, body):
            # The fake PNG carries the page number, so the body tells us which page this is.
            uri = body["document"]["image_url"]
            import base64

            page = int(base64.b64decode(uri.split(",", 1)[1]).rsplit(b"-", 1)[1])
            item = by_page[page]
            if isinstance(item, BaseException):
                raise item
            return item

        monkeypatch.setattr(cpc, "_invoke", fake_invoke)

        mapping = cpc.ocr_page_block_sync("/x.pdf", [1, 2, 3])

        assert sorted(mapping) == [1, 3]
        assert mapping[1].text == "one"
        assert mapping[3].text == ""

    def test_in_flight_requests_are_bounded_by_page_concurrency(self, monkeypatch) -> None:
        import threading
        import time

        monkeypatch.setenv("PDF_OCR_PAGE_CONCURRENCY", "2")
        lock = threading.Lock()
        state = {"in_flight": 0, "peak": 0}

        def fake_invoke(model, body):
            with lock:
                state["in_flight"] += 1
                state["peak"] = max(state["peak"], state["in_flight"])
            time.sleep(0.02)
            with lock:
                state["in_flight"] -= 1
            return _body({"pages": [{"blocks": []}]})

        monkeypatch.setattr(cpc, "_invoke", fake_invoke)

        mapping = cpc.ocr_page_block_sync("/x.pdf", list(range(1, 9)))

        assert len(mapping) == 8
        assert 1 <= state["peak"] <= 2

    def test_unopenable_file_yields_an_empty_mapping(self, monkeypatch) -> None:
        def explode(_path):
            raise OSError("no such file")

        monkeypatch.setattr(cpc, "_page_count", explode)

        assert cpc.ocr_page_block_sync("/missing.pdf", [1, 2]) == {}

    def test_a_group_renders_inside_the_worker_not_up_front(self, monkeypatch) -> None:
        """At most PDF_OCR_PAGE_CONCURRENCY page PNGs are resident, whatever the group size."""
        import threading

        monkeypatch.setenv("PDF_OCR_PAGE_CONCURRENCY", "2")
        lock = threading.Lock()
        state = {"resident": 0, "peak": 0}

        def fake_render(_path, page):
            with lock:
                state["resident"] += 1
                state["peak"] = max(state["peak"], state["resident"])
            return b"\x89PNG-fake-" + str(page).encode()

        def fake_invoke(model, body):
            with lock:
                state["resident"] -= 1
            return _body({"pages": [{"blocks": []}]})

        monkeypatch.setattr(cpc, "_render_page", fake_render)
        monkeypatch.setattr(cpc, "_invoke", fake_invoke)

        mapping = cpc.ocr_page_block_sync("/x.pdf", list(range(1, 17)))

        assert len(mapping) == 16
        assert state["peak"] <= 2

    def test_empty_selection_is_a_no_op(self, monkeypatch) -> None:
        calls = _capture_invoke(monkeypatch, [_body({"pages": []})])

        assert cpc.ocr_page_block_sync("/x.pdf", []) == {}
        assert calls == []


class TestMetering:
    def test_successful_requests_increment_the_engine_labelled_counter(self, monkeypatch, blocks_payload) -> None:
        from app.metrics import OCR_PAGES_TOTAL

        _capture_invoke(monkeypatch, [_body(blocks_payload)])
        counter = OCR_PAGES_TOTAL.labels(engine="cohere_parse")
        before = counter._value.get()

        cpc.ocr_page_block_sync("/x.pdf", [1, 2, 3])

        assert counter._value.get() == before + 3

    def test_failed_requests_are_not_metered(self, monkeypatch) -> None:
        from app.metrics import OCR_PAGES_TOTAL

        _capture_invoke(monkeypatch, [_http_error(422, "bad")])
        counter = OCR_PAGES_TOTAL.labels(engine="cohere_parse")
        before = counter._value.get()

        cpc.ocr_page_sync("/x.pdf", 1)

        assert counter._value.get() == before


class TestRetryLadder:
    """Back after a host change removed the thing that owned it.

    botocore retried throttles and 5xx before anything reached this adapter.
    httpx retries nothing. Had the retries not come back with the transport,
    the move to Cohere's API would have quietly pushed more pages onto
    Tesseract — a capability regression with no error anywhere to point at,
    because every one of those pages still "succeeded" via the fallback.

    These exercise ``_invoke`` for real and stub ``_post``, the innermost
    seam, so the retry decision itself is under test rather than mocked past.
    """

    @staticmethod
    def _responses(monkeypatch, items):
        """Stub ``_post``; each item is a fake response or an exception."""
        calls: list[dict] = []
        queue = list(items)

        class _FakeResponse:
            def __init__(self, status_code, content=b"{}", headers=None):
                self.status_code = status_code
                self.content = content
                self.text = content.decode(errors="replace")
                self.headers = headers or {}

        def fake_post(body):
            calls.append(body)
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, BaseException):
                raise item
            status, payload, headers = item
            return _FakeResponse(status, payload, headers)

        monkeypatch.setattr(cpc, "_post", fake_post)
        monkeypatch.setattr(cpc.time, "sleep", lambda _s: None)
        return calls, _FakeResponse

    def test_a_throttle_is_retried_and_then_succeeds(self, monkeypatch) -> None:
        calls, _ = self._responses(
            monkeypatch,
            [(429, b"slow down", {}), (200, b'{"pages": []}', {})],
        )

        assert cpc._invoke(MODEL, {"document": {}}) == b'{"pages": []}'
        assert len(calls) == 2

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_server_errors_are_retried(self, monkeypatch, status) -> None:
        calls, _ = self._responses(
            monkeypatch,
            [(status, b"oops", {}), (200, b'{"pages": []}', {})],
        )

        cpc._invoke(MODEL, {"document": {}})
        assert len(calls) == 2

    def test_a_refusal_is_not_retried(self, monkeypatch) -> None:
        """Retrying a 401 spends the page budget three times over to be told
        the same thing. The key is not going to become valid in 500ms."""
        calls, _ = self._responses(monkeypatch, [(401, b"invalid api token", {})])

        with pytest.raises(cpc.CohereParseHttpError) as exc:
            cpc._invoke(MODEL, {"document": {}})

        assert exc.value.status_code == 401
        assert len(calls) == 1

    def test_retries_are_bounded(self, monkeypatch) -> None:
        """A page is one unit of a bounded per-document budget. Retrying
        forever costs the whole document's OCR window on one bad page."""
        calls, _ = self._responses(monkeypatch, [(503, b"down", {})])

        with pytest.raises(cpc.CohereParseHttpError):
            cpc._invoke(MODEL, {"document": {}})

        assert len(calls) == cpc._MAX_ATTEMPTS

    def test_a_transport_error_is_retried_then_raised(self, monkeypatch) -> None:
        import httpx

        calls, _ = self._responses(monkeypatch, [httpx.ConnectError("no route")])

        with pytest.raises(httpx.ConnectError):
            cpc._invoke(MODEL, {"document": {}})

        assert len(calls) == cpc._MAX_ATTEMPTS

    def test_the_model_is_spliced_into_the_body(self, monkeypatch) -> None:
        """`model` is back in the body — the ADR-0023 half of the move."""
        calls, _ = self._responses(monkeypatch, [(200, b'{"pages": []}', {})])

        cpc._invoke(MODEL, {"document": {"type": "image_url"}, "output_format": "blocks"})

        assert calls[0]["model"] == MODEL
        assert set(calls[0]) == {"model", "document", "output_format"}

    def test_retry_after_is_honoured_when_sane(self, monkeypatch) -> None:
        slept: list[float] = []
        self._responses(
            monkeypatch,
            [(429, b"", {"retry-after": "2"}), (200, b'{"pages": []}', {})],
        )
        monkeypatch.setattr(cpc.time, "sleep", lambda s: slept.append(s))

        cpc._invoke(MODEL, {"document": {}})

        assert slept == [2.0]

    def test_an_absurd_retry_after_is_capped(self, monkeypatch) -> None:
        """Waiting minutes for one page is worse than falling to Tesseract
        and moving on — the budget is per document, not per page."""
        slept: list[float] = []
        self._responses(
            monkeypatch,
            [(429, b"", {"retry-after": "3600"}), (200, b'{"pages": []}', {})],
        )
        monkeypatch.setattr(cpc.time, "sleep", lambda s: slept.append(s))

        cpc._invoke(MODEL, {"document": {}})

        assert slept == [cpc._MAX_RETRY_AFTER_S]

    def test_a_garbage_retry_after_falls_back_to_backoff(self, monkeypatch) -> None:
        """`Retry-After` can be an HTTP-date, which float() will not parse."""
        slept: list[float] = []
        self._responses(
            monkeypatch,
            [(429, b"", {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}), (200, b'{"pages": []}', {})],
        )
        monkeypatch.setattr(cpc.time, "sleep", lambda s: slept.append(s))

        cpc._invoke(MODEL, {"document": {}})

        assert slept == [cpc._BACKOFF_BASE_S]
