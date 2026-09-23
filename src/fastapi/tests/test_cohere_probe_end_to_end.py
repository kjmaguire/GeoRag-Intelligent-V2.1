"""The Cohere probe must MEASURE, not merely run.

`test_cohere_probe_verdict.py` covers the verdict logic against synthetic
report dicts. This file runs the actual probe against a Cohere-shaped HTTP
server and asserts on what it reports — which is a different property, and
the one that matters: a probe whose sections all degrade cleanly and observe
nothing useful would pass every test in that file.

It found two real defects the first time it was run, both recorded in
`ops/validation/tests/fake_cohere.py`. Neither was the kind a reviewer
catches; both were the kind a single live call catches, which is the thesis
of the probe itself.

The load-bearing test is `test_it_catches_a_host_that_ignores_the_system_prompt`.
That failure returns 200, produces fluent text, and raises nothing anywhere —
so if the probe cannot see it, nothing in this system can.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_OPS = Path(__file__).resolve().parents[3] / "ops" / "validation"
sys.path.insert(0, str(_OPS))
sys.path.insert(0, str(_OPS / "tests"))

import cohere_probe  # noqa: E402
from fake_cohere import serve  # noqa: E402

FIXTURE_PDF = Path(__file__).parent / "fixtures" / "ocr" / "PLS-2024-Technical-Report.pdf"


@pytest.fixture
def probe(monkeypatch):
    """Point the probe at a local fake, in a chosen mode, and run it.

    Returns a callable so each test picks its own mode. The server binds
    port 0 so parallel runs cannot collide.
    """

    def _run(mode: str = "honest", *, pdf: Path | None = None) -> dict:
        monkeypatch.setenv("FAKE_COHERE_MODE", mode)
        server, _thread = serve(0)
        port = server.server_address[1]
        try:
            monkeypatch.setenv("COHERE_API_KEY", "test-only-not-a-real-cohere-key")
            monkeypatch.setenv("COHERE_BASE_URL", f"http://127.0.0.1:{port}")
            monkeypatch.delenv("COHERE_CHAT_MODEL", raising=False)
            monkeypatch.delenv("COHERE_PARSE_MODEL", raising=False)
            report = {
                "reachability": cohere_probe.probe_reachability(),
                "chat": cohere_probe.probe_chat(),
                "chat_stream": cohere_probe.probe_chat_stream(),
                "parse": cohere_probe.probe_parse(pdf, [1]) if pdf else {"skipped": "no pdf"},
                "latency": cohere_probe.probe_latency(1),
            }
            report["verdict"] = cohere_probe.verdict(report)
            report["contract_diff"] = cohere_probe._diff_report(report)
            return report
        finally:
            server.shutdown()
            server.server_close()

    return _run


class TestItObservesWhatItClaimsTo:
    def test_a_healthy_run_verifies_the_sections_it_reached(self, probe) -> None:
        report = probe()
        v = report["verdict"]
        assert v["verified_anything"] is True
        assert "chat" in v["sections_ok"]
        assert "chat_stream" in v["sections_ok"]

    def test_it_reads_the_json_mode_answer(self, probe) -> None:
        """Hard rule 4's dependency. Every typed-output guard assumes the
        model was actually asked for JSON."""
        chat = probe()["chat"]
        assert chat["with_response_format"]["parses_as_json"] is True

    def test_it_records_whether_the_sentinels_survive(self, probe) -> None:
        with_sentinels = probe()["chat"]["with_response_format"]
        assert with_sentinels["sentinels_present"] == ["<|START_TEXT|>", "<|END_TEXT|>"]

        without = probe("strips_sentinels")["chat"]["with_response_format"]
        assert without["sentinels_present"] == []
        # Still parses either way — clean_model_text strips unconditionally,
        # so the adapter does not care. The probe records it so a future
        # reader knows the stripping is dead code on this host rather than
        # guessing.
        assert without["parses_as_json"] is True

    def test_it_reads_the_stream_through_the_real_adapter(self, probe) -> None:
        stream = probe()["chat_stream"]
        assert stream["adapter_unavailable"] is None
        assert stream["adapter_read_any_delta"] is True
        assert stream["delta_shapes_seen"] == ["delta.message.content.text"]
        assert stream["text_chars"] > 0

    def test_it_reads_both_parse_formats_through_the_real_adapter(self, probe) -> None:
        if not FIXTURE_PDF.exists():
            pytest.skip("OCR fixture PDF not present")
        formats = probe(pdf=FIXTURE_PDF)["parse"]["formats"]
        for name in ("blocks", "markdown"):
            assert formats[name]["adapter_page_ok"]["ok"] is True, name
        assert formats["blocks"]["block_types"] == ["table", "text"]


class TestItCatchesTheFailuresItExistsFor:
    def test_it_catches_a_host_that_ignores_the_system_prompt(self, probe) -> None:
        """THE test in this file.

        Cohere v2 takes system as a MESSAGE; Bedrock Converse takes it as a
        top-level parameter. A host that quietly drops the system message
        returns 200 with fluent text and raises nothing — the grounding
        rules are simply never applied, while the citation guards go on
        enforcing against the output. Nothing else in this system can see
        that, so if the probe cannot, the check does not exist.
        """
        honest = probe()["chat"]["system_is_a_message"]
        assert honest["obeyed_the_system_prompt"] is True

        broken = probe("ignores_system")["chat"]["system_is_a_message"]
        assert broken["obeyed_the_system_prompt"] is False
        # A 200, which is the whole problem.
        assert broken["role_system_accepted"] is True
        assert "SILENT" in broken["note"]

    def test_an_unauthorized_key_is_named_as_such(self, probe) -> None:
        """ "Your key is wrong" and "the adapter is wrong" send an operator to
        different files."""
        report = probe("unauthorized")
        v = report["verdict"]
        assert v["verified_anything"] is False
        assert v["authentication_failed"] is True
        assert "COHERE_API_KEY" in v["summary"]

    def test_an_unauthorized_key_verifies_nothing_even_with_a_pdf(self, probe) -> None:
        """The test above passes no PDF, so Parse is skipped -- and that gap
        hid a real one. With a PDF, the pixel ladder recorded each 401 as a
        rung (``status``/``accepted: False``, no ``error``), the verdict read
        the rung as an observation, and a key Cohere refused outright came
        back "ok=parse", "verified". Found by running the probe through
        ops/rehearsal/run_cohere_probe.sh against this same fake."""
        report = probe("unauthorized", pdf=FIXTURE_PDF)
        v = report["verdict"]
        assert v["verified_anything"] is False, v["summary"]
        assert "parse" in v["sections_failed"]
        assert v["authentication_failed"] is True


class TestTheFirstLiveRunsFalseGreens:
    """Each of these read "ok" in the first run from inside the VPC
    (2026-09-23), whose verdict said "verified 4/4 sections" while the stream
    had parsed nothing and every Parse call had been refused."""

    def test_a_stream_with_no_readable_event_fails_the_section(self, probe) -> None:
        report = probe("unreadable_stream")
        stream = report["chat_stream"]
        assert stream["event_types"] == {}
        assert stream["error"]["type"] == "NoStreamEvents"
        assert stream["content_type"] == "application/octet-stream"
        assert stream["line_kinds"] == {"other": 1}
        assert "chat_stream" in report["verdict"]["sections_failed"]

    @pytest.mark.parametrize("mode", ["ndjson_stream", "whole_body_stream"])
    def test_other_framings_are_read_through_the_real_adapter(self, probe, mode) -> None:
        stream = probe(mode)["chat_stream"]
        assert "error" not in stream, stream.get("error")
        assert stream["text_chars"] > 0
        assert stream["adapter_read_any_delta"] is True

    def test_a_stream_is_requested_as_an_event_stream(self, probe) -> None:
        stream = probe()["chat_stream"]
        assert stream["content_type"] == "text/event-stream"
        assert stream["line_kinds"]["event"] == stream["line_kinds"]["data"] - 1  # [DONE] has no event line

    def test_the_object_form_of_image_url_fails_parse(self, probe, monkeypatch) -> None:
        """What the probe sent before the fix. The fake refuses it exactly as
        Cohere did, and the section must say failed, not ok."""
        if not FIXTURE_PDF.exists():
            pytest.skip("OCR fixture PDF not present")
        real = cohere_probe._parse_body

        def _object_form(model, png, output_format):
            body = real(model, png, output_format)
            body["document"]["image_url"] = {"url": body["document"]["image_url"]}
            return body

        monkeypatch.setattr(cohere_probe, "_parse_body", _object_form)
        report = probe(pdf=FIXTURE_PDF)
        assert "should be of type string" in report["parse"]["formats"]["blocks"]["error"]["message"]
        assert "parse" in report["verdict"]["sections_failed"], report["verdict"]["summary"]

    def test_the_parse_blocks_are_read_in_the_sdk_shape(self, probe) -> None:
        if not FIXTURE_PDF.exists():
            pytest.skip("OCR fixture PDF not present")
        blocks = probe(pdf=FIXTURE_PDF)["parse"]["formats"]["blocks"]
        assert blocks["block_keys"] == ["table", "text", "type"]
        assert "content" in blocks["block_payload_keys"]
        assert blocks["adapter_page_ok"]["chars"] > 0


class TestTheContractDiffIsWiredUp:
    def test_the_diff_resolves_and_observes_the_calls_it_reached(self, probe) -> None:
        diff = probe()["contract_diff"]
        assert "skipped" not in diff, "app.services.cohere_wire failed to import"
        assert "chat_v2" in diff["calls_observed"]

    def test_a_healthy_run_declares_everything_it_sees(self, probe) -> None:
        """An UNDECLARED field is the contract's discovery half working — but
        against a server shaped exactly like the declaration there should be
        none, and one appearing means the contract has drifted from what the
        adapters read.

        This is not hypothetical: on the first run of this harness EVERY
        Parse response field came back undeclared, because the contract
        carried twelve fields and no `evidence_key` for any of them.
        """
        if not FIXTURE_PDF.exists():
            pytest.skip("OCR fixture PDF not present")
        diff = probe(pdf=FIXTURE_PDF)["contract_diff"]
        assert diff["undeclared_fields"] == {}
        assert diff["required_fields_missing"] == {}
        assert diff["contract_holds"] is True
