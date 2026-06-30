"""Plan §0c — Qwen3-14B/30B-A3B citation compliance benchmark scaffold.

The entire four-guard citation system assumes Qwen3 reliably produces
structured citations in the format the prompts request. That assumption
is unverified. Plan §0c spells out six tests with explicit trial counts
and a decision gate: if Test 1 compliance < 85%, the system prompt must
be redesigned BEFORE any citation guard implementation proceeds.

This module is a **benchmark scaffold**, not a CI test:

  - Default state: marked ``@pytest.mark.qwen3_compliance`` AND skipped
    unconditionally with a clear reason — running these against a live
    vLLM endpoint takes ~10 minutes and costs real tokens. CI never
    fires these.

  - Manual run: the companion script ``scripts/run_qwen3_citation_compliance.py``
    lifts the skip via ``--manual`` and executes the 100 trials.

  - Pass/fail: each test computes a compliance rate over its trial
    count. Reports a structured result the runner can aggregate into
    a plan-§0c verdict (Test 1 ≥ 85% required to unblock §4b citation
    guards).

The fixtures are minimal real-world geological documents — short enough
to fit in context, structured enough to test exact-citation reproduction.
Trial counts come verbatim from plan §0c (20 / 20 / 20 / 10 / 10 / 20).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Skip-by-default — flip via env QWEN3_COMPLIANCE_MANUAL=1 (the runner script
# sets this) or directly via pytest -m qwen3_compliance --override-skip.
# ---------------------------------------------------------------------------

_MANUAL_RUN_ENABLED = os.environ.get("QWEN3_COMPLIANCE_MANUAL") == "1"

pytestmark = [
    pytest.mark.qwen3_compliance,
    pytest.mark.skipif(
        not _MANUAL_RUN_ENABLED,
        reason=(
            "Citation-compliance trials hit the live vLLM endpoint and take "
            "~10 minutes. Run via `python scripts/run_qwen3_citation_compliance.py` "
            "or set QWEN3_COMPLIANCE_MANUAL=1."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Result dataclass — surfaced into the runner script's report
# ---------------------------------------------------------------------------


@dataclass
class TrialResult:
    """One trial's outcome.

    ``compliant`` is the binary pass/fail of the structured-citation
    check; ``detail`` carries the raw response + scratch info for
    debugging.
    """

    compliant: bool
    detail: str = ""
    raw_response: str = ""


@dataclass
class SuiteResult:
    """Aggregate across a test's trials."""

    name: str
    trials: int
    compliant_count: int
    failures: list[TrialResult] = field(default_factory=list)

    @property
    def compliance_rate(self) -> float:
        return self.compliant_count / self.trials if self.trials else 0.0


# ---------------------------------------------------------------------------
# Fixtures: short geological documents + ground-truth citation answers
# ---------------------------------------------------------------------------


_DOC_A = (
    "[Document 1 — Property Description, NI 43-101 Eckville 2024, p.7]\n"
    "The Eckville property comprises 12 mineral claims totalling 248 hectares "
    "located in the Patricia Mining Division of Ontario. Surface access is via "
    "a 4.2 km logging spur off Highway 105."
)
_DOC_B = (
    "[Document 2 — Sampling & Analysis, NI 43-101 Eckville 2024, p.42]\n"
    "Drillhole ECK-22-001 returned 2.31 g/t Au over 8.4 m from 142.0 m to "
    "150.4 m downhole, including a higher-grade interval of 5.18 g/t Au over "
    "1.6 m from 145.2 m to 146.8 m. Samples were assayed at Activation "
    "Laboratories using fire assay with AA finish (code Au-AA23)."
)
_DOC_C = (
    "[Document 3 — Resource Estimate, NI 43-101 Eckville 2024, p.103]\n"
    "The Inferred Mineral Resource at the Eckville Main Zone, effective "
    "31 December 2023, totals 1.18 Moz Au at an average grade of 1.82 g/t Au, "
    "applying a cut-off grade of 0.50 g/t Au."
)


import pytest_asyncio


@pytest_asyncio.fixture
async def vllm_client():
    """Pooled vLLM HTTP client. Real connection — only fires under manual run.

    Async fixture so teardown can `await client.aclose()` inside the live
    pytest_asyncio event loop. Previous sync-fixture version did
    `asyncio.run(client.aclose())` AFTER pytest_asyncio had closed the
    loop, producing 6 spurious "Event loop is closed" teardown ERRORs
    in the 2026-05-27 benchmark run (audit:
    `docs/audits/qwen3_compliance_2026_05_27.md`).
    """
    pytest.importorskip("httpx")
    import httpx

    base = os.environ.get("VLLM_BASE_URL", "http://localhost:8000")
    client = httpx.AsyncClient(base_url=base, timeout=120.0)
    try:
        yield client
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _system_prompt_citation_section() -> str:
    """Returns the production system-prompt citation block (or the proposed
    plan-§4a block when no production version is wired yet).

    This is the central piece under test: every trial uses the same system
    prompt that production will use, so the compliance rate measures what
    real users will see.
    """
    # Lazy import so the test module loads without the FastAPI app
    # being importable in isolation.
    try:
        # If production exposes a citation-section builder, use it.
        # Today the plan-§4a draft prompt lives at
        # src/fastapi/app/agent/prompts/_drafts/structured_answer_format_v1.txt
        # — task #12 of the overnight run.
        return _PLAN_4A_DRAFT
    except Exception:
        return _PLAN_4A_DRAFT


_PLAN_4A_DRAFT = """\
Every factual claim in your answer must be followed by a citation in
the form [doc:<N> p:<page>] where N is the 1-indexed document number
and page matches the page shown in the document header.

If a claim has no supporting document, do not state it. If documents
disagree, cite each one and label the disagreement.
""".strip()


async def _one_shot(
    client: Any,
    *,
    documents: list[str],
    question: str,
    temperature: float = 0.1,
) -> str:
    """Single completion call against vLLM."""
    payload = {
        "model": os.environ.get("VLLM_MODEL", "Qwen/Qwen3-14B-AWQ"),
        "messages": [
            {"role": "system", "content": _system_prompt_citation_section()},
            {
                "role": "user",
                "content": "\n\n".join(documents) + f"\n\nQuestion: {question}",
            },
        ],
        "temperature": temperature,
        # Bumped from 400 → 1200 after the 2026-05-27 audit found
        # Qwen3-14B's <think>...</think> reasoning blocks consume the
        # response budget before the cited answer can land. See audit
        # docs/audits/qwen3_compliance_2026_05_27.md tests 4 + 5.
        "max_tokens": 1200,
    }
    resp = await client.post("/v1/chat/completions", json=payload)
    resp.raise_for_status()
    body = resp.json()
    return body["choices"][0]["message"]["content"]


def _has_doc_citation(response: str, doc_number: int) -> bool:
    """True if response references doc N in [doc:N ...] form."""
    import re

    return bool(re.search(rf"\[doc:\s*{doc_number}\b", response))


def _has_any_citation(response: str) -> bool:
    import re

    return bool(re.search(r"\[doc:\s*\d+", response))


def _references_invented_doc(response: str, max_real_doc: int) -> bool:
    """True if response cites a doc number > what was provided."""
    import re

    for m in re.finditer(r"\[doc:\s*(\d+)", response):
        if int(m.group(1)) > max_real_doc:
            return True
    return False


def _has_value(response: str, value: str) -> bool:
    return value.lower() in response.lower()


# ---------------------------------------------------------------------------
# Test 1 — Basic citation production (20 trials)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qwen3_test1_basic_citation_production(vllm_client):
    """Plan §0c Test 1 — provide 3 docs, ask about doc 2's content, check
    citation references doc 2 specifically. 20 trials, ≥85% compliance.
    """
    docs = [_DOC_A, _DOC_B, _DOC_C]
    question = "Where was hole ECK-22-001 assayed and what assay method was used?"

    suite = SuiteResult(name="test1_basic_citation", trials=20, compliant_count=0)
    for _ in range(suite.trials):
        response = await _one_shot(vllm_client, documents=docs, question=question)
        ok = _has_doc_citation(response, 2)
        suite.compliant_count += int(ok)
        if not ok:
            suite.failures.append(
                TrialResult(compliant=False, detail="missing [doc:2] citation",
                            raw_response=response[:400])
            )

    # Plan §0c gating: ≥ 85% required.
    assert suite.compliance_rate >= 0.85, _failure_message(suite, 0.85)


# ---------------------------------------------------------------------------
# Test 2 — Numeric citation grounding (20 trials)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qwen3_test2_numeric_citation_grounding(vllm_client):
    """Plan §0c Test 2 — provide assay data, ask for the Au value, verify
    citation includes doc ID, page reference, AND the exact value.
    """
    docs = [_DOC_A, _DOC_B, _DOC_C]
    question = "What Au grade did ECK-22-001 return over 8.4 m, and where is that recorded?"

    suite = SuiteResult(name="test2_numeric_grounding", trials=20, compliant_count=0)
    for _ in range(suite.trials):
        response = await _one_shot(vllm_client, documents=docs, question=question)
        ok = (
            _has_value(response, "2.31")
            and ("g/t" in response.lower())
            and _has_doc_citation(response, 2)
        )
        suite.compliant_count += int(ok)
        if not ok:
            suite.failures.append(
                TrialResult(compliant=False, detail="missing value/unit/citation",
                            raw_response=response[:400])
            )

    assert suite.compliance_rate >= 0.85, _failure_message(suite, 0.85)


# ---------------------------------------------------------------------------
# Test 3 — Hallucinated index detection (20 trials)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qwen3_test3_hallucinated_index_detection(vllm_client):
    """Plan §0c Test 3 — provide 3 docs, ask about a topic in none of them.
    Verify model either refuses OR cites correctly; MUST NOT invent doc:4.
    """
    docs = [_DOC_A, _DOC_B, _DOC_C]
    question = "What is the company's environmental tailings management plan?"

    suite = SuiteResult(name="test3_no_hallucinated_index", trials=20, compliant_count=0)
    for _ in range(suite.trials):
        response = await _one_shot(vllm_client, documents=docs, question=question)
        invented = _references_invented_doc(response, max_real_doc=3)
        # Compliance: did not invent a doc index. Refusal is fine; citing
        # a real doc that doesn't actually contain the answer is also
        # surfaced by other guards — not in scope here.
        ok = not invented
        suite.compliant_count += int(ok)
        if not ok:
            suite.failures.append(
                TrialResult(compliant=False, detail="invented doc index",
                            raw_response=response[:400])
            )

    assert suite.compliance_rate >= 0.85, _failure_message(suite, 0.85)


# ---------------------------------------------------------------------------
# Test 4 — Multi-document citation (10 trials)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qwen3_test4_multi_document_citation(vllm_client):
    """Plan §0c Test 4 — provide 5 docs (3 with relevant evidence). Verify
    citations reference ALL 3 contributing docs.
    """
    docs = [
        _DOC_A,
        _DOC_B,
        _DOC_C,
        (
            "[Document 4 — Project Summary, Fact Sheet 2024, p.1]\n"
            "Eckville Main Zone Inferred Resource: 1.18 Moz Au at 1.82 g/t."
        ),
        (
            "[Document 5 — Press Release, 2024-03-12]\n"
            "Drillhole ECK-22-001 returned 2.31 g/t Au over 8.4 m at the Eckville Main Zone."
        ),
    ]
    question = (
        "Summarise the Eckville Main Zone resource, the best ECK-22-001 intercept, "
        "and the property location."
    )

    suite = SuiteResult(name="test4_multi_doc_citation", trials=10, compliant_count=0)
    for _ in range(suite.trials):
        response = await _one_shot(vllm_client, documents=docs, question=question)
        # Compliance: at least 3 distinct doc numbers cited.
        import re
        cited = {int(m.group(1)) for m in re.finditer(r"\[doc:\s*(\d+)", response)}
        ok = len(cited) >= 3
        suite.compliant_count += int(ok)
        if not ok:
            suite.failures.append(
                TrialResult(compliant=False,
                            detail=f"only {len(cited)} distinct docs cited: {sorted(cited)}",
                            raw_response=response[:400])
            )

    assert suite.compliance_rate >= 0.85, _failure_message(suite, 0.85)


# ---------------------------------------------------------------------------
# Test 5 — Long-context citation drift (10 trials)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qwen3_test5_long_context_drift(vllm_client):
    """Plan §0c Test 5 — fill context to ~5,500 tokens with documents + query.
    Ask about a doc in the first 1,000 tokens. Verify citation accuracy
    does NOT degrade.
    """
    # Build a long context: target doc first, then lorem-padding.
    # IMPORTANT: rewrite _DOC_B's in-document header from "Document 2" to
    # "Document 1" for this test. The shared fixture is labelled "Document 2"
    # because it sits at position 1 in tests 1-4's 3-doc setup; here it's
    # at position 1, so the in-document label must match the list position
    # or the model correctly cites "[doc:2]" by reading the label, failing
    # the test's `_has_doc_citation(response, 1)` check.
    doc_b_as_doc1 = _DOC_B.replace(
        "[Document 2 — Sampling & Analysis,",
        "[Document 1 — Sampling & Analysis,",
    )
    padding = (
        "[Document N — Filler]\n" + "Property description continues. " * 200
    )
    docs = [doc_b_as_doc1] + [padding for _ in range(8)]  # doc 1 is the assay doc
    question = "What Au grade did ECK-22-001 return over 8.4 m? Cite the document."

    suite = SuiteResult(name="test5_long_context_drift", trials=10, compliant_count=0)
    for _ in range(suite.trials):
        response = await _one_shot(vllm_client, documents=docs, question=question)
        ok = _has_doc_citation(response, 1) and _has_value(response, "2.31")
        suite.compliant_count += int(ok)
        if not ok:
            suite.failures.append(
                TrialResult(compliant=False, detail="cited wrong doc or missing value",
                            raw_response=response[:400])
            )

    assert suite.compliance_rate >= 0.85, _failure_message(suite, 0.85)


# ---------------------------------------------------------------------------
# Test 6 — Citation format under structured-answer format (20 trials)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qwen3_test6_citation_under_structured_format(vllm_client):
    """Plan §0c Test 6 — apply the plan §4a structured answer format
    (8 sections). Verify citations appear in the correct position
    (Evidence section).
    """
    docs = [_DOC_A, _DOC_B, _DOC_C]
    question = "What is the Inferred Resource at the Eckville Main Zone? Use the structured format."

    # Use the structured-answer prompt instead of the plain citation block.
    # This swaps the system prompt for the trial duration.
    structured_prompt = (
        _system_prompt_citation_section()
        + "\n\nStructure your answer in these sections:\n"
        + "1. Direct answer\n2. Key numbers (with citations)\n"
        + "3. Evidence (quoted source text + citation)\n"
        + "4. Source citation\n5. Assumptions\n6. Confidence\n"
        + "7. What is missing or uncertain\n8. Suggested follow-up questions"
    )

    suite = SuiteResult(name="test6_structured_format", trials=20, compliant_count=0)
    for _ in range(suite.trials):
        payload = {
            "model": os.environ.get("VLLM_MODEL", "Qwen/Qwen3-14B-AWQ"),
            "messages": [
                {"role": "system", "content": structured_prompt},
                {"role": "user",
                 "content": "\n\n".join(docs) + f"\n\nQuestion: {question}"},
            ],
            "temperature": 0.1,
            # See max_tokens=1200 note in _one_shot — same reasoning,
            # structured-format answers are even longer.
            "max_tokens": 1500,
        }
        resp = await vllm_client.post("/v1/chat/completions", json=payload)
        resp.raise_for_status()
        response = resp.json()["choices"][0]["message"]["content"]

        # Compliance: response contains an Evidence section AND a
        # citation appears inside it (rather than only in the Key numbers
        # section).
        lines = response.lower().splitlines()
        in_evidence = False
        evidence_has_citation = False
        for line in lines:
            if "evidence" in line and not line.startswith("    "):
                in_evidence = True
                continue
            if in_evidence and "source citation" in line:
                break
            if in_evidence and "[doc:" in line:
                evidence_has_citation = True
                break

        ok = evidence_has_citation
        suite.compliant_count += int(ok)
        if not ok:
            suite.failures.append(
                TrialResult(compliant=False, detail="no citation in Evidence section",
                            raw_response=response[:400])
            )

    assert suite.compliance_rate >= 0.85, _failure_message(suite, 0.85)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _failure_message(suite: SuiteResult, threshold: float) -> str:
    return (
        f"\n{suite.name}: compliance {suite.compliance_rate:.0%} "
        f"({suite.compliant_count}/{suite.trials}) below threshold {threshold:.0%}.\n"
        + (
            "First failures:\n  - "
            + "\n  - ".join(
                f"{f.detail}: {f.raw_response[:200]}..."
                for f in suite.failures[:3]
            )
            if suite.failures
            else ""
        )
    )
