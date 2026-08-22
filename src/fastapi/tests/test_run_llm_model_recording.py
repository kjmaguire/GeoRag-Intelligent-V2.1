"""`query_audit_log.llm_model` must name the model that actually answered.

It did not, for every query ever logged. Two independent reasons stacked:

1. Laravel stamps the column at reservation time from ``config('services.
   fastapi.llm_model')`` — before any model has been selected, let alone
   called. That value is a configuration statement, not an observation.

2. The refresh meant to correct it read a ``routing`` SSE frame carrying
   ``{tier, model, reason}``. Nothing emitted that frame. The producer
   lived in the flat ``app/agent/orchestrator.py`` module and did not
   survive that module becoming the ``app/agent/orchestrator/`` package;
   the only remaining trace is the literal ``__routing__:local_llm:``
   inside a stale May-14 ``.pyc``. Both consumers — here and in Laravel —
   stayed put, so the pipeline read as wired from either end.

The replacement carries the model on ``GeoRAGResponse.llm_model``, which
rides the terminal ``completed`` frame Laravel already reads for text,
citations and confidence.

These tests pin the three things that can silently break it again: that an
answer call records, that a classifier call does not, and that no new
``audit_label`` can appear without a decision about which of those two it
is.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.agent.llm_calls import (
    NON_ANSWER_AUDIT_LABELS,
    get_run_llm_model,
    record_run_llm_model,
    reset_run_llm_model,
)
from app.models.rag import Citation, GeoRAGResponse

APP_ROOT = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture(autouse=True)
def _clean_contextvar():
    reset_run_llm_model()
    yield
    reset_run_llm_model()


# ---------------------------------------------------------------------------
# The contextvar itself
# ---------------------------------------------------------------------------


def test_no_model_recorded_before_any_call():
    assert get_run_llm_model() is None


def test_recording_a_model_makes_it_readable():
    record_run_llm_model("Cohere-command-a-plus-05-2026")
    assert get_run_llm_model() == "Cohere-command-a-plus-05-2026"


def test_a_later_answer_call_overwrites_an_earlier_one():
    # After a repair pass or a failover, the model that SERVED is the last
    # one to run, not the first.
    record_run_llm_model("model-a")
    record_run_llm_model("model-b")
    assert get_run_llm_model() == "model-b"


@pytest.mark.parametrize("empty", [None, ""])
def test_an_empty_model_never_clobbers_a_real_one(empty):
    record_run_llm_model("model-a")
    record_run_llm_model(empty)
    assert get_run_llm_model() == "model-a"


# ---------------------------------------------------------------------------
# Label classification — the part that actually decides what gets recorded
# ---------------------------------------------------------------------------


def _audit_labels_in_tree() -> set[str]:
    """Every `audit_label=` literal passed anywhere under app/."""
    found: set[str] = set()
    for path in APP_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "audit_label" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, str):
                        found.add(kw.value.value)
    return found


#: Labels whose output CAN become the answer the user reads. Kept here
#: rather than imported so the test states the expectation independently of
#: the implementation — NON_ANSWER_AUDIT_LABELS is the denylist under test,
#: and asserting a denylist against itself proves nothing.
ANSWER_LABELS = {"agentic_retrieval", "agentic_retrieval_repair_stage3"}
NON_ANSWER_LABELS = {"intent_classifier"}


def test_every_audit_label_is_classified():
    """A new label must not silently inherit a default.

    The denylist in llm_calls.py treats anything unlisted as
    answer-producing. That is the right default — a new synthesis path
    records automatically — but it means a new CLASSIFIER-ish label would
    quietly start writing the wrong model into an audit column. This test
    is the thing that stops it: add a label, and you are made to say which
    side of the line it is on.
    """
    actual = _audit_labels_in_tree()
    classified = ANSWER_LABELS | NON_ANSWER_LABELS
    unclassified = actual - classified

    assert not unclassified, (
        f"New audit_label(s) {sorted(unclassified)} appeared under app/ "
        f"without being classified.\n\n"
        f"Decide whether each one can produce the answer the user reads:\n"
        f"  - it can     -> add it to ANSWER_LABELS here; nothing else to do, "
        f"the denylist already records it.\n"
        f"  - it cannot  -> add it to NON_ANSWER_LABELS here AND to "
        f"NON_ANSWER_AUDIT_LABELS in app/agent/llm_calls.py, or it will "
        f"overwrite query_audit_log.llm_model with a model that did not "
        f"write the answer."
    )

    # The reverse direction: a label removed from the code should be removed
    # from this list too, so it does not sit here implying coverage.
    stale = classified - actual
    assert not stale, (
        f"These audit_label values are classified here but no longer appear "
        f"under app/: {sorted(stale)}. Remove them."
    )


def test_the_denylist_matches_the_labels_classified_as_non_answer():
    assert set(NON_ANSWER_AUDIT_LABELS) == NON_ANSWER_LABELS


def test_answer_labels_are_not_on_the_denylist():
    assert not (ANSWER_LABELS & set(NON_ANSWER_AUDIT_LABELS))


def test_the_documented_label_vocabulary_matches_reality():
    """The docstring's label list is load-bearing; keep it true.

    It listed "primary", "retry", "failover", "follow_ups" and "classifier"
    — five strings that appear nowhere in this codebase. The first draft of
    the recording logic allowlisted three of them and therefore recorded
    nothing at all, which is how this test came to exist.
    """
    from app.agent.llm_calls import _call_llm

    doc = _call_llm.__doc__ or ""
    for label in _audit_labels_in_tree():
        assert label in doc, (
            f'audit_label "{label}" is used in app/ but is not listed in '
            f"_call_llm's docstring."
        )


# ---------------------------------------------------------------------------
# The wire contract Laravel depends on
# ---------------------------------------------------------------------------


def _response(**overrides) -> GeoRAGResponse:
    payload = {
        "text": "Granodiorite intersected at 120 m [1].",
        "citations": [
            Citation(
                citation_id="1",
                citation_type="NI43",
                source_chunk_id="chunk-1",
                document_title="hole-1.pdf",
                page=1,
                relevance_score=0.9,
            )
        ],
        "confidence": 0.8,
        "sources_used": ["chunk-1"],
    }
    payload.update(overrides)
    return GeoRAGResponse(**payload)


def test_completed_payload_carries_llm_model():
    dumped = _response(llm_model="Cohere-command-a-plus-05-2026").model_dump()
    assert dumped["llm_model"] == "Cohere-command-a-plus-05-2026"


def test_llm_model_is_optional_so_refusals_still_validate():
    # A refusal can assemble without ever reaching an answer-producing call.
    # NULL is the honest value there; a required field would force a lie.
    assert _response().llm_model is None
    assert "llm_model" in _response().model_dump()


def test_assemble_response_stamps_the_recorded_model():
    from app.agent.response_assembler import assemble_response

    record_run_llm_model("Cohere-command-a-plus-05-2026")
    response = assemble_response(
        "Granodiorite intersected at 120 m [1].",
        [("retrieve_qdrant", {"chunks": [], "data_source": "qdrant"})],
    )
    assert response.llm_model == "Cohere-command-a-plus-05-2026"


def test_assemble_response_leaves_the_model_null_when_nothing_answered():
    from app.agent.response_assembler import assemble_response

    response = assemble_response(
        "Granodiorite intersected at 120 m [1].",
        [("retrieve_qdrant", {"chunks": [], "data_source": "qdrant"})],
    )
    assert response.llm_model is None


def test_the_recorded_model_uses_the_same_expression_as_the_wire():
    """Guard the one way this can go quietly wrong again: drift.

    Both LLM paths choose what to send with ``model or
    settings.effective_llm_model`` (llm_calls.py, in
    ``_call_openai_compatible_llm`` and ``_call_anthropic_llm``). The
    recording site uses that identical expression, so the name written to
    query_audit_log.llm_model cannot diverge from the name actually put on
    the wire without both moving together through
    ``settings.effective_llm_model``.

    A runtime-value assertion would not catch that, and would not even run
    meaningfully here: no LLM backend is configured in the test
    environment, so ``settings.effective_llm_model`` is the empty string.
    """
    source = (APP_ROOT / "agent" / "llm_calls.py").read_text(encoding="utf-8")
    expression = "model or settings.effective_llm_model"

    # Two wire sites plus the recording site.
    assert source.count(expression) >= 3, (
        f"Expected the wire sites and the recording site to share "
        f"`{expression}`; found {source.count(expression)} occurrence(s). "
        f"If model resolution moved, move the recording with it."
    )
    assert f"record_run_llm_model({expression})" in source


# ---------------------------------------------------------------------------
# The dead protocol is gone from both ends
# ---------------------------------------------------------------------------


def test_the_routing_sentinel_is_gone_from_fastapi():
    source = (APP_ROOT / "routers" / "queries.py").read_text(encoding="utf-8")

    # The string may still appear in the comment explaining the removal;
    # what must not survive is code acting on it.
    code_lines = [
        line for line in source.splitlines()
        if "__routing__" in line and not line.lstrip().startswith("#")
    ]
    assert not code_lines, (
        "queries.py still has live code handling the __routing__ sentinel:\n  "
        + "\n  ".join(code_lines)
    )

    assert not re.search(r'_stamped_event\(\s*"routing"', source), (
        "queries.py still emits a `routing` SSE frame. Nothing produces the "
        "sentinel that would trigger it and nothing consumes the event."
    )
