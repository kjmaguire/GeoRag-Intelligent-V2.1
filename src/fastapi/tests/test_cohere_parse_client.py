"""Cohere Parse adapter — wire shape, response adaptation, failure modes.

No network: the single seam is ``cohere_parse_client._post``, replaced with
a fake that returns hand-rolled responses (the same idiom as
test_foundry_retry.py). Rendering is replaced too, so these tests do not
need a PDF; the render path has its own tests in test_cohere_parse_pixel_cap.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from app.services import _foundry_retry as retry_mod
from app.services.ingest import cohere_parse_client as cpc

FIXTURES = Path(__file__).parent / "fixtures" / "cohere_parse"


class _Resp:
    def __init__(self, status_code: int, payload=None, headers=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.request = httpx.Request("POST", "https://example.invalid/parse")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"status {self.status_code}", request=self.request, response=self)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("OCR_ENGINE", "cohere_parse")
    monkeypatch.setenv("AZURE_FOUNDRY_ENDPOINT", "https://foundry.example.invalid/")
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "k-secret")
    monkeypatch.setenv("AZURE_FOUNDRY_PARSE_DEPLOYMENT", "Cohere-parse-v5")
    monkeypatch.delenv("COHERE_PARSE_OUTPUT_FORMAT", raising=False)
    monkeypatch.delenv("COHERE_PARSE_INCLUDE_IMAGE_DESCRIPTIONS", raising=False)
    monkeypatch.setattr(retry_mod.time, "sleep", lambda _s: None)
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


def _capture_post(monkeypatch, responses):
    calls: list[dict] = []
    queue = list(responses)

    def fake_post(url, headers, body):
        calls.append({"url": url, "headers": headers, "body": body})
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(cpc, "_post", fake_post)
    return calls


class TestSelectionAndConfiguration:
    def test_engine_is_selected_by_ocr_engine_value(self, monkeypatch) -> None:
        assert cpc.is_engine_selected()
        monkeypatch.setenv("OCR_ENGINE", "tesseract")
        assert not cpc.is_engine_selected()

    def test_is_configured_needs_all_three(self, monkeypatch) -> None:
        assert cpc.is_configured()
        monkeypatch.delenv("AZURE_FOUNDRY_PARSE_DEPLOYMENT")
        assert not cpc.is_configured()

    def test_missing_config_raises_not_configured_at_call_time(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv("AZURE_FOUNDRY_API_KEY")

        with pytest.raises(cpc.CohereParseNotConfigured):
            cpc.ocr_page_sync("/x.pdf", 1)
        with pytest.raises(cpc.CohereParseNotConfigured):
            cpc.ocr_page_block_sync("/x.pdf", [1, 2])

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
    def test_request_goes_to_the_parse_path_with_api_key_and_data_uri(
        self, monkeypatch, blocks_payload
    ) -> None:
        calls = _capture_post(monkeypatch, [_Resp(200, blocks_payload)])

        cpc.ocr_page_sync("/x.pdf", 3)

        assert (
            calls[0]["url"]
            == "https://foundry.example.invalid/providers/cohere/v2/parse"
        )
        assert calls[0]["headers"] == {"api-key": "k-secret"}
        body = calls[0]["body"]
        assert set(body) == {"model", "document", "output_format"}
        assert body["model"] == "Cohere-parse-v5"
        assert body["document"]["type"] == "image_url"
        assert body["document"]["image_url"]["url"].startswith("data:image/png;base64,")
        assert body["output_format"] == "blocks"

    def test_output_format_env_reaches_the_body_and_invalid_falls_back(
        self, monkeypatch, blocks_payload, caplog
    ) -> None:
        calls = _capture_post(monkeypatch, [_Resp(200, blocks_payload)])
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
        _capture_post(monkeypatch, [_Resp(200, blocks_payload)])

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
        _capture_post(monkeypatch, [_Resp(200, blocks_payload)])
        monkeypatch.setenv("COHERE_PARSE_INCLUDE_IMAGE_DESCRIPTIONS", "1")

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert (
            "[Figure: Plan view map of the Madison deposit showing drill collars.]"
            in result.text
        )

    def test_markdown_mode_strips_image_refs_and_converts_html_tables(
        self, monkeypatch, markdown_payload
    ) -> None:
        _capture_post(monkeypatch, [_Resp(200, markdown_payload)])

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert result.request_succeeded
        assert "![" not in result.text
        assert "<table>" not in result.text
        assert "img_1" not in result.text
        assert len(result.tables) == 1
        assert result.tables[0][0] == ["Category", "Tonnes (Mt)", "Au (g/t)"]
        assert "Indicated" in result.text

    def test_markdown_as_a_plain_string_is_accepted(self, monkeypatch) -> None:
        _capture_post(
            monkeypatch, [_Resp(200, {"pages": [{"markdown": "Just prose."}]})]
        )

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert result.text == "Just prose."
        assert result.tables == []

    def test_an_empty_page_is_a_success_with_no_text(self, monkeypatch) -> None:
        _capture_post(monkeypatch, [_Resp(200, {"pages": [{"blocks": []}]})])

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert result.request_succeeded
        assert result.text == ""

    def test_page_text_is_stripped_so_joiner_arithmetic_stays_exact(
        self, monkeypatch
    ) -> None:
        _capture_post(
            monkeypatch,
            [
                _Resp(
                    200,
                    {"pages": [{"blocks": [{"type": "text", "text": "  hello \n\n"}]}]},
                )
            ],
        )

        assert cpc.ocr_page_sync("/x.pdf", 1).text == "hello"


class TestFailureModes:
    def test_non_retryable_4xx_fails_soft_with_the_status(self, monkeypatch) -> None:
        _capture_post(monkeypatch, [_Resp(400, {"message": "image too large"})])

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert not result.request_succeeded
        assert result.error.startswith("400:")
        assert "image too large" in result.error
        assert result.confidence_reported is False

    def test_403_is_logged_at_error(self, monkeypatch, caplog) -> None:
        _capture_post(monkeypatch, [_Resp(403, {"message": "quota"})])

        with caplog.at_level(logging.ERROR, logger="georag.ingest.cohere_parse"):
            result = cpc.ocr_page_sync("/x.pdf", 1)

        assert not result.request_succeeded
        assert any(
            r.levelno == logging.ERROR and "403" in r.getMessage()
            for r in caplog.records
        )

    def test_429_is_retried_then_succeeds(self, monkeypatch, blocks_payload) -> None:
        calls = _capture_post(
            monkeypatch,
            [_Resp(429, headers={"retry-after": "0"}), _Resp(200, blocks_payload)],
        )

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert result.request_succeeded
        assert len(calls) == 2

    def test_exhausted_retries_fail_soft(self, monkeypatch) -> None:
        _capture_post(monkeypatch, [_Resp(503)])

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert not result.request_succeeded
        assert result.error.startswith("503:")

    def test_transport_error_fails_soft(self, monkeypatch) -> None:
        def boom(url, headers, body):
            raise httpx.ConnectError("down")

        monkeypatch.setattr(cpc, "_post", boom)

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert not result.request_succeeded
        assert "down" in result.error

    def test_non_json_body_fails_soft(self, monkeypatch) -> None:
        _capture_post(monkeypatch, [_Resp(200, None, text="<html>gateway</html>")])

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert not result.request_succeeded
        assert result.error.startswith("non_json_response")

    def test_render_failure_fails_soft_without_a_request(self, monkeypatch) -> None:
        calls = _capture_post(monkeypatch, [_Resp(200, {"pages": []})])
        monkeypatch.setattr(cpc, "_render_page", lambda _path, page: None)

        result = cpc.ocr_page_sync("/x.pdf", 1)

        assert not result.request_succeeded
        assert result.error == "render_failed"
        assert calls == []


class TestPageGroups:
    def test_group_posts_one_request_per_page_keyed_by_absolute_page(
        self, monkeypatch, blocks_payload
    ) -> None:
        calls = _capture_post(monkeypatch, [_Resp(200, blocks_payload)])

        mapping = cpc.ocr_page_block_sync("/x.pdf", [7, 3, 3, 12])

        assert sorted(mapping) == [3, 7, 12]
        assert len(calls) == 3
        assert all(r.request_succeeded for r in mapping.values())

    def test_a_failed_page_is_absent_and_an_empty_page_is_present(
        self, monkeypatch
    ) -> None:
        by_page = {
            1: _Resp(200, {"pages": [{"blocks": [{"type": "text", "text": "one"}]}]}),
            2: _Resp(400, {"message": "bad"}),
            3: _Resp(200, {"pages": [{"blocks": []}]}),
        }

        def fake_post(url, headers, body):
            # The fake PNG carries the page number, so the body tells us which page this is.
            uri = body["document"]["image_url"]["url"]
            import base64

            page = int(base64.b64decode(uri.split(",", 1)[1]).rsplit(b"-", 1)[1])
            return by_page[page]

        monkeypatch.setattr(cpc, "_post", fake_post)

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

        def fake_post(url, headers, body):
            with lock:
                state["in_flight"] += 1
                state["peak"] = max(state["peak"], state["in_flight"])
            time.sleep(0.02)
            with lock:
                state["in_flight"] -= 1
            return _Resp(200, {"pages": [{"blocks": []}]})

        monkeypatch.setattr(cpc, "_post", fake_post)

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

        def fake_post(url, headers, body):
            with lock:
                state["resident"] -= 1
            return _Resp(200, {"pages": [{"blocks": []}]})

        monkeypatch.setattr(cpc, "_render_page", fake_render)
        monkeypatch.setattr(cpc, "_post", fake_post)

        mapping = cpc.ocr_page_block_sync("/x.pdf", list(range(1, 17)))

        assert len(mapping) == 16
        assert state["peak"] <= 2

    def test_empty_selection_is_a_no_op(self, monkeypatch) -> None:
        calls = _capture_post(monkeypatch, [_Resp(200, {"pages": []})])

        assert cpc.ocr_page_block_sync("/x.pdf", []) == {}
        assert calls == []


class TestMetering:
    def test_successful_requests_increment_the_engine_labelled_counter(
        self, monkeypatch, blocks_payload
    ) -> None:
        from app.metrics import OCR_PAGES_TOTAL

        _capture_post(monkeypatch, [_Resp(200, blocks_payload)])
        counter = OCR_PAGES_TOTAL.labels(engine="cohere_parse")
        before = counter._value.get()

        cpc.ocr_page_block_sync("/x.pdf", [1, 2, 3])

        assert counter._value.get() == before + 3

    def test_failed_requests_are_not_metered(self, monkeypatch) -> None:
        from app.metrics import OCR_PAGES_TOTAL

        _capture_post(monkeypatch, [_Resp(400, {"message": "bad"})])
        counter = OCR_PAGES_TOTAL.labels(engine="cohere_parse")
        before = counter._value.get()

        cpc.ocr_page_sync("/x.pdf", 1)

        assert counter._value.get() == before
