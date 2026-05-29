"""Regression tests for the SSE `completed` event carrying the persisted
silver.answer_runs.answer_run_id.

Background — the Retrieval Inspector deep link (/retrieval/{id}) was broken
because the Reverb `completed` event used to stamp the streaming-session
UUID owned by EventStamper instead of the DB row's answer_run_id. The
streaming-session UUID is only meaningful as a Redis ring-buffer key during
the request — it is never persisted to PostgreSQL, so the controller's
`silver.answer_runs.where(answer_run_id = …)` lookup always returned 0
rows and the inspector page rendered the empty state.

Fix (see app/models/rag.py + app/agent/orchestrator/__init__.py +
app/routers/queries.py):

  1. GeoRAGResponse gained an optional `answer_run_id: UUID | None` field.
  2. run_deterministic_rag stamps `response.answer_run_id` from the
     persisted row id after INSERT.
  3. _stamped_event in queries.py prefers a payload-supplied
     `answer_run_id` over `stamper.answer_run_id` so the `completed` frame
     surfaces the real DB id.

These tests pin that contract.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from app.agent.event_stamper import EventStamper
from app.models.rag import Citation, GeoRAGResponse


def _make_response(answer_run_id: UUID | None) -> GeoRAGResponse:
    return GeoRAGResponse(
        text="The Wyoming Roll-Front deposit dips north-east.",
        answer_run_id=answer_run_id,
        citations=[
            Citation(
                citation_id="[DATA-1]",
                citation_type="DATA",
                source_chunk_id="chunk-abc",
                document_title="NI 43-101 Shirley Basin",
                relevance_score=0.92,
            )
        ],
        confidence=0.9,
        sources_used=["chunk-abc"],
    )


class TestGeoRAGResponseAnswerRunIdField:
    """The model contract that fans out into the SSE payload."""

    def test_field_defaults_to_none(self) -> None:
        response = _make_response(answer_run_id=None)
        assert response.answer_run_id is None

    def test_field_accepts_uuid(self) -> None:
        run_id = uuid4()
        response = _make_response(answer_run_id=run_id)
        assert response.answer_run_id == run_id

    def test_field_accepts_uuid_string(self) -> None:
        run_id = uuid4()
        response = _make_response(answer_run_id=str(run_id))  # type: ignore[arg-type]
        assert response.answer_run_id == run_id

    def test_model_dump_round_trip_preserves_run_id(self) -> None:
        run_id = uuid4()
        response = _make_response(answer_run_id=run_id)
        dumped = response.model_dump()
        assert "answer_run_id" in dumped
        # The orchestrator pushes the dumped dict into _stamped_event,
        # which calls str(...) before emitting. Confirm UUID survives the
        # default model_dump (python mode) so the str() coercion works.
        assert dumped["answer_run_id"] == run_id
        assert str(dumped["answer_run_id"]) == str(run_id)


def _override_logic(data: dict[str, Any], stamper: EventStamper) -> str:
    """Mirror of the inline override block in app/routers/queries.py.

    Kept in lockstep with _stamped_event so this test pins the contract
    without spinning up the full FastAPI app. If queries.py changes the
    precedence rule, update both sides.
    """
    payload_run_id = data.get("answer_run_id")
    if payload_run_id is not None:
        return str(payload_run_id)
    return str(stamper.answer_run_id)


class TestStampedEventAnswerRunIdPrecedence:
    """The SSE-frame override that surfaces the DB id to the frontend."""

    def test_payload_supplied_run_id_overrides_stamper(self) -> None:
        # Streaming-session UUID — what the stamper alone would emit.
        stream_run_id = uuid4()
        stamper = EventStamper(answer_run_id=stream_run_id)
        # DB row id — what the orchestrator persisted to silver.answer_runs.
        db_run_id = uuid4()

        response = _make_response(answer_run_id=db_run_id)
        emitted = _override_logic(response.model_dump(), stamper)

        assert emitted == str(db_run_id)
        assert emitted != str(stream_run_id), (
            "Regression: the streaming-session UUID leaked back through "
            "after the override — the Retrieval Inspector deep link will "
            "404 again."
        )

    def test_missing_payload_run_id_falls_back_to_stamper(self) -> None:
        # Pre-INSERT refusal paths (LLM health probe / out-of-scope) return
        # GeoRAGResponse(answer_run_id=None). The stamper UUID is the only
        # id we have — accept it as the fallback rather than emit None.
        stream_run_id = uuid4()
        stamper = EventStamper(answer_run_id=stream_run_id)

        response = _make_response(answer_run_id=None)
        emitted = _override_logic(response.model_dump(), stamper)

        assert emitted == str(stream_run_id)

    def test_non_completed_frames_unaffected(self) -> None:
        # status / delta / citation frames carry no answer_run_id field;
        # they should always pick up the stamper UUID.
        stream_run_id = uuid4()
        stamper = EventStamper(answer_run_id=stream_run_id)
        status_payload = {"message": "Analyzing query…"}
        delta_payload = {"token": "hello ", "token_seq": 1}

        assert _override_logic(status_payload, stamper) == str(stream_run_id)
        assert _override_logic(delta_payload, stamper) == str(stream_run_id)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
