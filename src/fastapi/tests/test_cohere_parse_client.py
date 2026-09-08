"""Cohere Parse adapter — wire shape, response adaptation, failure modes.

No network: the single seam is ``cohere_parse_client._invoke``, replaced with
a fake that returns hand-rolled response bodies. Rendering is replaced too, so
these tests do not need a PDF; the render path has its own tests in
test_cohere_parse_pixel_cap.

Rewritten 2026-09-08 for ADR-0022 (Foundry → Bedrock). The seam changed shape
— ``_invoke(model_id, body) -> bytes`` instead of
``_post(url, headers, body) -> httpx.Response`` — but the adapter under test
did not: the request body is still Cohere's own, minus ``model``, and the
response adapter is untouched. That is the migration's central claim about
this file, and these tests are what makes it checkable.

One class of test is deliberately gone: the retry ladder. botocore owns retry
now, so by the time a ThrottlingException reaches this adapter it has already
been retried and the only question left is whether the fallback is clean.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from app.services.ingest import cohere_parse_client as cpc

FIXTURES = Path(__file__).parent / "fixtures" / "cohere_parse"

MODEL_ID = "arn:aws:sagemaker:us-east-1:123456789012:endpoint/cohere-parse-v5"


def _body(payload) -> bytes:
    """A successful InvokeModel response body."""
    return json.dumps(payload).encode()


def _client_error(code: str, message: str = "") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}}, "InvokeModel"
    )


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("OCR_ENGINE", "cohere_parse")
    monkeypatch.setenv("BEDROCK_PARSE_MODEL_ID", MODEL_ID)
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
    monkeypatch.setattr(
        cpc, "_render_page", lambda _path, page: b"\x89PNG-fake-" + str(page).encode()
    )


@pytest.fixture
def blocks_payload():
    return json.loads((FIXTURES / "blocks_page.json").read_text())


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

    def fake_invoke(model_id, body):
        calls.append({"model_id": model_id, "body": body})
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

    def test_is_configured_needs_the_model_id(self, monkeypatch) -> None:
        assert cpc.is_configured()
        monkeypatch.delenv("BEDROCK_PARSE_MODEL_ID")
        assert not cpc.is_configured()

    def test_missing_config_raises_not_configured_at_call_time(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv("BEDROCK_PARSE_MODEL_ID")

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
    def test_request_carries_the_model_id_and_a_data_uri(
        self, monkeypatch, blocks_payload
    ) -> None:
        """The model moves from the body to modelId; nothing else changes.

        This assertion is the migration's central claim about this adapter:
        the request body is still Cohere's own parse body, so the response
        adapter below did not have to be touched.
        """
        calls = _capture_invoke(monkeypatch, [_body(blocks_payload)])

        cpc.ocr_page_sync("/x.pdf", 3)

        assert calls[0]["model_id"] == MODEL_ID
        body = calls[0]["body"]
        assert set(body) == {"document", "output_format"}
        assert "model" not in body, "model belongs in modelId, not the body"
        assert body["document"]["type"] == "image_url"
        assert body["document"]["image_url"]["url"].startswith("data:image/png;base64,")
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
        assert any(
            "COHERE_PARSE_OUTPUT_FORMAT" in r.getMessage() for r in caplog.records
        )


class TestResponseAdapter:
    def test_blocks_become_text_in_order_with_a_table_grid(
        self, monkeypatch, blocks_payload
    ) -> None:
        _capture_invoke(monkeypatch, [_body(blocks_payload)])

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert result.request_succeeded
        assert result.confidence_reported is False
        assert result.words == ()
        assert result.mean_confidence == 0.0
        assert result.detected_region_count == 0
        assert result.text.startswith("# 14 MINERAL RESOURCE ESTIMATES")
        assert result.text.rstrip().endswith(
            "Mineral resources are not mineral reserves."
        )
        # The table is both a grid (for the table sections) and inline text.
        assert len(result.tables) == 1
        grid = result.tables[0]
        assert grid[0] == ["Category", "Tonnes (Mt)", "Grade", "Grade"]
        assert grid[1] == ["Category", "Tonnes (Mt)", "Au (g/t)", "Ag (g/t)"]
        assert grid[4] == ["Inferred", "0.8", "1.1", "7.5"]
        assert "Inferred" in result.text and "7.5" in result.text
        # Image descriptions stay out by default.
        assert "Plan view map" not in result.text

    def test_image_descriptions_are_opt_in(self, monkeypatch, blocks_payload) -> None:
        _capture_invoke(monkeypatch, [_body(blocks_payload)])
        monkeypatch.setenv("COHERE_PARSE_INCLUDE_IMAGE_DESCRIPTIONS", "1")

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert (
            "[Figure: Plan view map of the Madison deposit showing drill collars.]"
            in result.text
        )

    def test_markdown_mode_strips_image_refs_and_converts_html_tables(
        self, monkeypatch, markdown_payload
    ) -> None:
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
        _capture_invoke(
            monkeypatch, [_body({"pages": [{"markdown": "Just prose."}]})]
        )

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert result.text == "Just prose."
        assert result.tables == []

    def test_an_empty_page_is_a_success_with_no_text(self, monkeypatch) -> None:
        _capture_invoke(monkeypatch, [_body({"pages": [{"blocks": []}]})])

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert result.request_succeeded
        assert result.text == ""

    def test_page_text_is_stripped_so_joiner_arithmetic_stays_exact(
        self, monkeypatch
    ) -> None:
        _capture_invoke(
            monkeypatch,
            [_body({"pages": [{"blocks": [{"type": "text", "text": "  hello \n\n"}]}]})],
        )

        assert cpc.ocr_page_sync("/x.pdf", 1).text == "hello"


class TestFailureModes:
    def test_a_rejected_request_fails_soft_with_the_error_code(
        self, monkeypatch
    ) -> None:
        _capture_invoke(
            monkeypatch,
            [_client_error("ValidationException", "image too large")],
        )

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert not result.request_succeeded
        assert result.error.startswith("ValidationException:")
        assert "image too large" in result.error
        assert result.confidence_reported is False

    @pytest.mark.parametrize(
        "code", ["AccessDeniedException", "ResourceNotFoundException"]
    )
    def test_a_denied_call_is_logged_at_error(self, monkeypatch, caplog, code) -> None:
        """The Foundry equivalent was an HTTP 403, and it earned its level:
        Foundry blocked 1,421 of 2,524 calls on 2026-08-17 and nothing
        noticed. Bedrock also surfaces this in InvocationClientErrors, but
        the log line is the only place the page and the model id meet."""
        _capture_invoke(monkeypatch, [_client_error(code, "nope")])

        with caplog.at_level(logging.ERROR, logger="georag.ingest.cohere_parse"):
            result = cpc.ocr_page_sync("/x.pdf", 1)

        assert not result.request_succeeded
        assert any(
            r.levelno == logging.ERROR and code in r.getMessage()
            for r in caplog.records
        )

    def test_throttling_fails_soft_at_warning(self, monkeypatch, caplog) -> None:
        """botocore has already exhausted its adaptive retries by here, so a
        throttle that reaches this adapter is real — but it is weather, not
        an operator error, so it does not get the ERROR level a denial does."""
        _capture_invoke(monkeypatch, [_client_error("ThrottlingException", "slow down")])

        with caplog.at_level(logging.DEBUG, logger="georag.ingest.cohere_parse"):
            result = cpc.ocr_page_sync("/x.pdf", 1)

        assert not result.request_succeeded
        assert result.error.startswith("ThrottlingException:")
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_transport_error_fails_soft(self, monkeypatch) -> None:
        def boom(model_id, body):
            raise OSError("down")

        monkeypatch.setattr(cpc, "_invoke", boom)

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert not result.request_succeeded
        assert "down" in result.error

    def test_non_json_body_fails_soft(self, monkeypatch) -> None:
        """Kept distinct from a transport failure on purpose: "Bedrock
        refused" and "Bedrock answered with something that is not JSON" want
        different operator responses."""
        _capture_invoke(monkeypatch, [b"<html>gateway</html>"])

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert not result.request_succeeded
        assert result.error.startswith("non_json_response")

    def test_render_failure_fails_soft_without_a_request(self, monkeypatch) -> None:
        calls = _capture_invoke(monkeypatch, [_body({"pages": []})])
        monkeypatch.setattr(cpc, "_render_page", lambda _path, page: None)

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert not result.request_succeeded
        assert result.error == "render_failed"
        assert calls == []


class TestPageGroups:
    def test_group_posts_one_request_per_page_keyed_by_absolute_page(
        self, monkeypatch, blocks_payload
    ) -> None:
        calls = _capture_invoke(monkeypatch, [_body(blocks_payload)])

        mapping = cpc.ocr_page_block_sync("/x.pdf", [7, 3, 3, 12])

        assert sorted(mapping) == [3, 7, 12]
        assert len(calls) == 3
        assert all(r.request_succeeded for r in mapping.values())

    def test_a_failed_page_is_absent_and_an_empty_page_is_present(
        self, monkeypatch
    ) -> None:
        by_page = {
            1: _body({"pages": [{"blocks": [{"type": "text", "text": "one"}]}]}),
            2: _client_error("ValidationException", "bad"),
            3: _body({"pages": [{"blocks": []}]}),
        }

        def fake_invoke(model_id, body):
            # The fake PNG carries the page number, so the body tells us which page this is.
            uri = body["document"]["image_url"]["url"]
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

    def test_in_flight_requests_are_bounded_by_page_concurrency(
        self, monkeypatch
    ) -> None:
        import threading
        import time

        monkeypatch.setenv("PDF_OCR_PAGE_CONCURRENCY", "2")
        lock = threading.Lock()
        state = {"in_flight": 0, "peak": 0}

        def fake_invoke(model_id, body):
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

        def fake_invoke(model_id, body):
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
    def test_successful_requests_increment_the_engine_labelled_counter(
        self, monkeypatch, blocks_payload
    ) -> None:
        from app.metrics import OCR_PAGES_TOTAL

        _capture_invoke(monkeypatch, [_body(blocks_payload)])
        counter = OCR_PAGES_TOTAL.labels(engine="cohere_parse")
        before = counter._value.get()

        cpc.ocr_page_block_sync("/x.pdf", [1, 2, 3])

        assert counter._value.get() == before + 3

    def test_failed_requests_are_not_metered(self, monkeypatch) -> None:
        from app.metrics import OCR_PAGES_TOTAL

        _capture_invoke(monkeypatch, [_client_error("ValidationException", "bad")])
        counter = OCR_PAGES_TOTAL.labels(engine="cohere_parse")
        before = counter._value.get()

        cpc.ocr_page_sync("/x.pdf", 1)

        assert counter._value.get() == before
