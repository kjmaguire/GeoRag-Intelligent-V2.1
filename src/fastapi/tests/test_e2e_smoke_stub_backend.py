"""The money-path smoke's stand-in backend must be able to produce a cited answer.

The smoke runs RERANKER_BACKEND=cross_encoder and LLM_BACKEND=vllm against
tests/e2e_smoke/stub_backend.py, and until 2026-10-10 neither stand-in could
get its query to a cited answer:

* search_documents holds the cross_encoder backend to
  RERANKER_SCORE_THRESHOLD on the RAW logit. From 2026-10-04 the stub scored
  a pair only by the share of the question's words the passage held; the
  fixture's PLS-22-08 sections held 3 of the smoke query's 8 ("the", "hole",
  "pls") and scored -0.75, under the 0.0 floor with everything else, so the
  query was a Layer 1 refusal.
* The canned answer cited [DATA-1], which names no document passage (those
  are [NI43-n]). Layer 2 removes a sentence that rests only on a marker with
  no Citation behind it and refuses an answer left empty, so with retrieval
  working the answer would have been an unsupported_by_sources refusal
  instead.

The smoke's old pass rule accepted a refusal; test_e2e_smoke_query_oracle.py
is why it no longer does, and this file is why the rig gives it a cited
answer to check. Pages stand in for the ingested passages: pypdfium2 reads
the fixture's text layer the way pdf_report does, so there is no parser
subprocess, no database, no Qdrant and no network.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pypdfium2 as pdfium
import pytest

from app.agent.hallucination.layer2_typed_output import validate_and_repair_with_findings
from app.agent.response_assembler import assemble_response
from app.agent.tools import DocumentChunk, DocumentSearchResult
from app.config import settings

_FASTAPI = Path(__file__).resolve().parents[1]
_STUB = _FASTAPI / "tests" / "e2e_smoke" / "stub_backend.py"
_QUERY_LEG = _FASTAPI / "scripts" / "ci" / "e2e_smoke_query.py"
_FIXTURE = _FASTAPI / "tests" / "fixtures" / "ocr" / "PLS-2024-Technical-Report.pdf"

#: Same shape ingest_pdf._stable_report_id mints (uuid5 -> lowercase hyphenated).
INGESTED_REPORT = "5d0c4e0e-6f8e-5a64-9c3f-1f6f7b2f0a11"


def _load(path: Path, name: str) -> Iterator[ModuleType]:
    """Load a script that is not importable as a package, the way the oracle
    test loads e2e_smoke_query.py."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


@pytest.fixture(scope="module")
def stub() -> Iterator[ModuleType]:
    yield from _load(_STUB, "e2e_smoke_stub_under_test")


@pytest.fixture(scope="module")
def query_leg() -> Iterator[ModuleType]:
    yield from _load(_QUERY_LEG, "e2e_smoke_query_leg_under_test")


@pytest.fixture(scope="module")
def smoke_query() -> str:
    """The query leg's own default ``--query``, read rather than copied."""
    match = re.search(r'"--query",\s*default="([^"]+)"', _QUERY_LEG.read_text(encoding="utf-8"))
    assert match, "e2e_smoke_query.py no longer declares a default --query"
    return match.group(1)


@pytest.fixture(scope="module")
def pages() -> list[str]:
    pdf = pdfium.PdfDocument(str(_FIXTURE))
    try:
        return [pdf[n].get_textpage().get_text_bounded() for n in range(len(pdf))]
    finally:
        pdf.close()


def _scores(stub: ModuleType, query: str, texts: list[str]) -> list[float]:
    # What search_documents sends a non-hosted reranker: each passage cut to
    # RERANKER_INPUT_CHAR_BUDGET.
    budget = settings.RERANKER_INPUT_CHAR_BUDGET
    return [stub._rerank_logit(query, text[:budget]) for text in texts]


def _about(stub: ModuleType, query: str, pages: list[str]) -> tuple[list[str], list[str]]:
    """Pages that share a marker phrase with the query, and the rest."""
    markers = [m for m in stub._MARKER_PHRASES if m in query.lower()]
    assert markers, f"the smoke query {query!r} names none of the stub's marker phrases"
    hits = [text for text in pages if any(m in text.lower() for m in markers)]
    return hits, [text for text in pages if text not in hits]


# --------------------------------------------------------------------------- #
# The reranker                                                                 #
# --------------------------------------------------------------------------- #


def test_the_pages_the_query_is_about_clear_the_cross_encoder_floor(stub, smoke_query, pages):
    hits, _ = _about(stub, smoke_query, pages)
    assert hits, "the fixture no longer contains what the smoke's query asks about"

    scores = _scores(stub, smoke_query, hits)
    assert all(s >= settings.RERANKER_SCORE_THRESHOLD for s in scores), (
        f"the stub scores the passages the smoke's query is about at {scores}, "
        f"under RERANKER_SCORE_THRESHOLD={settings.RERANKER_SCORE_THRESHOLD}: "
        "search_documents drops them all and the smoke's query is refused"
    )


def test_the_other_pages_stay_under_it(stub, smoke_query, pages):
    """The floor still has something to drop, so the smoke still exercises it."""
    _, rest = _about(stub, smoke_query, pages)
    assert rest

    scores = _scores(stub, smoke_query, rest)
    assert all(s < settings.RERANKER_SCORE_THRESHOLD for s in scores), scores


def test_word_overlap_alone_would_not_clear_it(stub, smoke_query, pages, monkeypatch):
    """The regression the first test catches. With the marker rule off, which
    is the stub as it was until 2026-10-10, the pages the query is about
    score under the floor and nothing is retrieved."""
    hits, _ = _about(stub, smoke_query, pages)
    monkeypatch.setattr(stub, "_MARKER_PHRASES", [])

    scores = _scores(stub, smoke_query, hits)
    assert max(scores) < settings.RERANKER_SCORE_THRESHOLD, scores


# --------------------------------------------------------------------------- #
# The canned answer                                                            #
# --------------------------------------------------------------------------- #


def _retrieved(pages: list[str]) -> DocumentSearchResult:
    """search_documents' result over the passages the reranker lets through."""
    chunks = [
        DocumentChunk(
            chunk_id=f"3f2c9d1e-0000-4000-8000-00000000000{n}",
            text=text[: settings.RERANKER_INPUT_CHAR_BUDGET],
            source_document_id=INGESTED_REPORT,
            document_title="PLS-2024 Technical Report",
            section_number=str(n),
            section_title="Drilling",
            section="Drilling",
            page=n,
            document_type="NI43",
            report_id=INGESTED_REPORT,
            relevance_score=0.95,
        )
        for n, text in enumerate(pages, start=1)
    ]
    return DocumentSearchResult(chunks=chunks, count=len(chunks), data_source="Qdrant georag_chunks")


def _answer_frame(answer: str, docs: DocumentSearchResult) -> tuple[dict, list[str]]:
    """The answer as assemble_node builds it and Layer 2 leaves it."""
    response, findings = validate_and_repair_with_findings(assemble_response(answer, [("search_documents", docs)]))
    return response.model_dump(mode="json"), findings


def test_the_canned_answer_is_a_cited_answer_the_smoke_accepts(stub, query_leg, smoke_query, pages):
    hits, _ = _about(stub, smoke_query, pages)
    frame, findings = _answer_frame(stub._SSE_ANSWER, _retrieved(hits))

    assert findings == [], findings
    assert frame["refusal_payload"] is None
    assert query_leg.judge_completed(frame, report_id=INGESTED_REPORT) == []


def test_an_answer_citing_no_retrieved_passage_is_refused(stub, query_leg, smoke_query, pages):
    """The regression the test above catches: the canned answer as it was
    until 2026-10-10, [DATA-1] over a document-only retrieval."""
    hits, _ = _about(stub, smoke_query, pages)
    frame, findings = _answer_frame(stub._SSE_ANSWER.replace("[NI43-1]", "[DATA-1]"), _retrieved(hits))

    assert findings, "Layer 2 no longer reports a marker that names no citation"
    assert frame["refusal_payload"]["reason_code"] == "unsupported_by_sources"
    assert query_leg.judge_completed(frame, report_id=INGESTED_REPORT)
