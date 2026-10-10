"""The CI money-path smoke's pass/fail rule must be able to say "fail".

scripts/ci/e2e_smoke_query.py is the blocking "E2E money-path smoke" job's
query leg. Its rule used to be "the `completed` frame has a citation whose
`source_chunk_id` is truthy". `GeoRAGResponse.citations` has `min_length=1`, so
the assembler fills the slot with a SENTINEL (`no-tool-call`,
`georag_reports:empty`) when nothing was retrieved -- which makes a Layer 1
refusal, the exact outcome of broken retrieval, satisfy that rule. The job
printed "OK" on a stack that retrieved nothing.

Every frame below is produced by the REAL assembler / refusal builders, not
typed by hand, so the oracle is judged against what FastAPI actually emits.
The script itself is not importable as a package (scripts/ci has no
__init__), hence the load-by-path fixture, the same way the other script
tests here do it.

No database, no Qdrant, no network.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.agent.hallucination.layer1_retrieval import (
    build_refusal_payload,
    build_refusal_text,
)
from app.agent.response_assembler import EMPTY_SOURCE_SENTINELS, assemble_response
from app.agent.tools import DocumentChunk, DocumentSearchResult

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "ci" / "e2e_smoke_query.py"
)

#: Same shape ingest_pdf._stable_report_id mints (uuid5 -> lowercase hyphenated).
INGESTED_REPORT = "5d0c4e0e-6f8e-5a64-9c3f-1f6f7b2f0a11"
OTHER_REPORT = "9b1f3a52-0d7e-5c2a-8e44-3a9d6c1b7e02"


@pytest.fixture(scope="module")
def smoke():
    spec = importlib.util.spec_from_file_location("e2e_smoke_query_under_test", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def _chunk(report_id: str, n: int = 1) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=f"3f2c9d1e-0000-4000-8000-00000000000{n}",
        text="Hole PLS-22-08 intersected high-grade uranium mineralisation.",
        source_document_id=report_id,
        document_title="PLS-2024 Technical Report",
        section_number="14",
        section_title="Mineral Resource Estimate",
        section="Mineral Resource Estimate",
        page=3,
        document_type="NI43",
        report_id=report_id,
        relevance_score=0.91,
    )


def _frame(response: Any) -> dict[str, Any]:
    """What the router emits as the `completed` data: final.model_dump()."""
    return response.model_dump(mode="json")


def _cited_answer(report_id: str) -> dict[str, Any]:
    docs = DocumentSearchResult(
        chunks=[_chunk(report_id)], count=1, data_source="Qdrant georag_chunks",
    )
    return _frame(
        assemble_response(
            "Hole PLS-22-08 returned high-grade uranium mineralisation. [NI43-1]",
            [("search_documents", docs)],
        )
    )


def _layer1_refusal(tool_results: list[tuple[str, Any]]) -> dict[str, Any]:
    """assemble_node's Layer 1 hard-gate branch, verbatim."""
    response = assemble_response(build_refusal_text(), tool_results)
    response.refusal_payload = build_refusal_payload()
    return _frame(response)


# --------------------------------------------------------------------------- #
# The passing case -- without it the failing cases prove nothing.             #
# --------------------------------------------------------------------------- #


def test_a_cited_answer_from_the_ingested_report_passes(smoke):
    frame = _cited_answer(INGESTED_REPORT)

    assert frame["refusal_payload"] is None
    assert frame["citations"][0]["source_chunk_id"].startswith(
        f"georag_reports:{INGESTED_REPORT}:"
    ), "fixture drifted: the assembler no longer mints georag_reports:<report_id>:... ids"
    assert smoke.judge_completed(frame, report_id=INGESTED_REPORT) == []


def test_report_id_comparison_ignores_case(smoke):
    frame = _cited_answer(INGESTED_REPORT)
    assert smoke.judge_completed(frame, report_id=INGESTED_REPORT.upper()) == []


# --------------------------------------------------------------------------- #
# The failures this gate exists to catch.                                     #
# --------------------------------------------------------------------------- #


def test_layer1_refusal_with_no_tool_results_fails(smoke):
    """Broken retrieval, nothing came back at all: `no-tool-call` sentinel."""
    frame = _layer1_refusal([])

    # The shape that fooled the old rule: one citation, truthy chunk id.
    assert len(frame["citations"]) == 1
    assert frame["citations"][0]["source_chunk_id"] == "no-tool-call"
    assert frame["refusal_payload"]["reason_code"] == "insufficient_evidence"

    problems = smoke.judge_completed(frame, report_id=INGESTED_REPORT)

    assert any("REFUSAL" in p for p in problems), problems
    assert any("sentinel" in p for p in problems), problems


def test_layer1_refusal_after_an_empty_document_search_fails(smoke):
    """Search ran and found nothing: `georag_reports:empty` sentinel."""
    empty = DocumentSearchResult(chunks=[], count=0, data_source="Qdrant georag_chunks")
    frame = _layer1_refusal([("search_documents", empty)])

    assert frame["citations"][0]["source_chunk_id"] == "georag_reports:empty"

    problems = smoke.judge_completed(frame, report_id=INGESTED_REPORT)

    assert any("REFUSAL" in p for p in problems), problems
    assert any("sentinel" in p for p in problems), problems


def test_an_ungrounded_answer_with_only_the_placeholder_citation_fails(smoke):
    """Not a refusal, but nothing retrieved: the assembler's `no-tool-call` slot."""
    frame = _frame(assemble_response("Uranium is a radioactive element.", []))

    assert frame["refusal_payload"] is None
    assert [c["source_chunk_id"] for c in frame["citations"]] == ["no-tool-call"]

    problems = smoke.judge_completed(frame, report_id=INGESTED_REPORT)

    assert problems and any("sentinel" in p for p in problems), problems


def test_a_refusal_fails_even_when_it_carries_a_real_chunk_citation(smoke):
    """refusal_payload alone is disqualifying; real-looking citations don't rescue it."""
    frame = _cited_answer(INGESTED_REPORT)
    frame["refusal_payload"] = build_refusal_payload()

    problems = smoke.judge_completed(frame, report_id=INGESTED_REPORT)

    assert len(problems) == 1 and "REFUSAL" in problems[0], problems


def test_a_citation_from_a_different_report_fails(smoke):
    """Real evidence, wrong document: retrieval did not find what ingest wrote."""
    frame = _cited_answer(OTHER_REPORT)

    problems = smoke.judge_completed(frame, report_id=INGESTED_REPORT)

    assert len(problems) == 1, problems
    assert INGESTED_REPORT in problems[0]


def test_a_structured_tool_citation_is_not_proof_the_report_was_found(smoke):
    frame = _cited_answer(INGESTED_REPORT)
    frame["citations"] = [
        {**frame["citations"][0], "source_chunk_id": "silver.collars:count=7:first=abc"}
    ]

    problems = smoke.judge_completed(frame, report_id=INGESTED_REPORT)

    assert len(problems) == 1 and INGESTED_REPORT in problems[0], problems


@pytest.mark.parametrize("citations", [[], None])
def test_zero_citations_fails(smoke, citations):
    frame = _cited_answer(INGESTED_REPORT)
    frame["citations"] = citations

    problems = smoke.judge_completed(frame, report_id=INGESTED_REPORT)

    assert any("ZERO citations" in p for p in problems), problems


def test_a_citation_without_a_chunk_id_is_not_real_evidence(smoke):
    frame = _cited_answer(INGESTED_REPORT)
    frame["citations"] = [{**frame["citations"][0], "source_chunk_id": ""}]

    assert smoke.judge_completed(frame, report_id=INGESTED_REPORT)


def test_one_real_citation_among_sentinels_passes(smoke):
    """Mixed frames are legitimate (a structured tool came back empty beside a hit)."""
    frame = _cited_answer(INGESTED_REPORT)
    placeholder = {**frame["citations"][0], "source_chunk_id": "silver.collars:miss"}
    frame["citations"] = [placeholder, *frame["citations"]]

    assert smoke.judge_completed(frame, report_id=INGESTED_REPORT) == []


# --------------------------------------------------------------------------- #
# main(): the exit code CI actually reads, over a real-shaped SSE stream.     #
# --------------------------------------------------------------------------- #


def _run_main(smoke, monkeypatch, frame: dict[str, Any], *, report_id: str) -> int:
    body = (
        'event: status\ndata: {"message": "searching"}\n\n'
        f"event: completed\ndata: {json.dumps(frame)}\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/queries"
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        smoke.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "k" * 40)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "e2e_smoke_query.py",
            "--project-id", "11111111-1111-4111-8111-111111111111",
            "--workspace-id", "a0000000-0000-0000-0000-000000000001",
            "--report-id", report_id,
        ],
    )  # fmt: skip
    return smoke.main()


def test_main_exits_zero_on_a_cited_answer(smoke, monkeypatch, capsys):
    code = _run_main(
        smoke, monkeypatch, _cited_answer(INGESTED_REPORT), report_id=INGESTED_REPORT
    )

    assert code == 0, capsys.readouterr().err
    assert "query: OK" in capsys.readouterr().out


def test_main_exits_one_on_a_layer1_refusal_stream(smoke, monkeypatch, capsys):
    """The regression: this stream used to print "OK" and exit 0."""
    code = _run_main(smoke, monkeypatch, _layer1_refusal([]), report_id=INGESTED_REPORT)

    captured = capsys.readouterr()
    assert code == 1
    assert "query: OK" not in captured.out
    assert "REFUSAL" in captured.err


def test_report_id_is_a_required_argument(smoke, monkeypatch):
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "k" * 40)
    monkeypatch.setattr(
        sys,
        "argv",
        ["e2e_smoke_query.py", "--project-id", "p", "--workspace-id", "w"],
    )

    with pytest.raises(SystemExit) as exc_info:
        smoke.main()

    assert exc_info.value.code == 2


# --------------------------------------------------------------------------- #
# Drift guard: the sentinel set is the producer's, not a copy.                #
# --------------------------------------------------------------------------- #


def test_every_producer_sentinel_is_rejected(smoke):
    """A sentinel the assembler mints must never count as a real citation."""
    for sentinel in sorted(EMPTY_SOURCE_SENTINELS):
        frame = _cited_answer(INGESTED_REPORT)
        frame["citations"] = [{**frame["citations"][0], "source_chunk_id": sentinel}]

        problems = smoke.judge_completed(frame, report_id=INGESTED_REPORT)

        assert any("sentinel" in p for p in problems), (sentinel, problems)


def test_the_script_uses_the_producers_sentinel_set(smoke):
    assert smoke.EMPTY_SOURCE_SENTINELS is EMPTY_SOURCE_SENTINELS
