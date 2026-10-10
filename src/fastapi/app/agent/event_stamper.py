"""Per-answer-run SSE event sequencer — Module 7 Phase B Chunk 1.

EventStamper assigns a monotonically increasing event_seq and a unique
event_id UUID to every SSE frame emitted during a single answer run.  The
stamper is instantiated once per request inside _agent_rag_stream() and
threaded forward via argument — no global state.

event_seq (int, 1-based):
    Event-level sequence counter.  Every SSE frame — status, routing, delta,
    citation, completed, failed — increments this counter.  The ``delta``
    event already carries an internal ``seq`` (per-token counter renamed
    ``token_seq``); that counter is preserved and does not replace event_seq.

event_id (str UUID4):
    Unique per-frame identifier.  The Module 7 UI uses this as the
    idempotency key when replaying events from the Redis ring buffer:
    a set of seen event_ids prevents duplicate processing on reconnect.

trace_id (str | None):
    OTel trace identifier.  Module 10 owns OTel instrumentation.  Until
    Module 10 wires a real trace_id, this field is None on every event so
    the frontend can handle it as optional.  The EventStamper accepts an
    optional trace_id at construction time so Module 10 can inject it without
    changing the callers.

Redis ring buffer (per answer run):
    Key:  georag:answer_run_events:<answer_run_id>
    Op:   RPUSH + EXPIRE(3600) on every emit call, pipelined (one round trip)
    Alias: on ``completed`` the list is COPY'd under the persisted
           silver.answer_runs id, which is the id the replay endpoint
           authorises and reads by (see :meth:`EventStamper.alias_to`).
    TTL:  3600 seconds — covers realistic reconnect window (seconds-to-minutes)

Architecture reference: Module spec §07f addendum (event_seq / event_id).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from app.config import settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "georag:answer_run_events"
_TTL_S = 3600


@dataclass
class EventStamper:
    """Per-answer-run event sequencer.  Owned by a single SSE handler; monotonic.

    Instantiate once per request:

        stamper = EventStamper(answer_run_id=answer_run_id)

    Then stamp every event before emission:

        seq, eid = stamper.next()
        enriched = {**data, "event_seq": seq, "event_id": eid,
                    "answer_run_id": str(stamper.answer_run_id),
                    "trace_id": stamper.trace_id}

    Persist + emit via stamper.push_to_redis(redis, event_name, enriched).
    """

    answer_run_id: UUID
    trace_id: str | None = None
    _seq: int = field(default=0, init=False, repr=False)
    #: Set by the first failed or timed-out Redis write. From then on this
    #: stream writes nothing: the replay buffer is a recovery aid, every frame
    #: awaits it, and a Redis that is down must cost a stream ONE bounded stall,
    #: not one per token.
    _redis_failed: bool = field(default=False, init=False, repr=False)

    def next(self) -> tuple[int, str]:
        """Increment sequence and return (event_seq, event_id).

        Returns:
            (event_seq, event_id) — event_seq is 1-based int, event_id is UUID4 str.
        """
        self._seq += 1
        return self._seq, str(uuid4())

    @property
    def current_seq(self) -> int:
        """The last emitted event_seq (0 if no events emitted yet)."""
        return self._seq

    async def push_to_redis(
        self,
        redis: Any,
        event_name: str,
        enriched: dict[str, Any],
    ) -> None:
        """Persist an enriched event dict to the Redis ring buffer.

        Writes atomically: RPUSH to append, then EXPIRE to reset the 1-hour TTL.
        Both calls are awaited in sequence (two round-trips per event is fine;
        see spec — simplicity over micro-optimisation for V1).

        Silently skips if redis is None (unit-test / dev path without Redis).

        Args:
            redis:      redis.asyncio.Redis instance (from app.state.redis_client).
            event_name: SSE event name (e.g. "delta", "citation", "completed").
            enriched:   Full event payload dict including event_seq / event_id.
        """
        if redis is None or self._redis_failed:
            return
        key = f"{_KEY_PREFIX}:{self.answer_run_id}"
        try:
            serialized = json.dumps(enriched, ensure_ascii=False, default=str)
            # One round trip, not two (API-2): this runs once per SSE frame
            # — every delta token — on the streaming hot path.
            pipe = redis.pipeline(transaction=False)
            pipe.rpush(key, serialized)
            pipe.expire(key, _TTL_S)
            # Bounded by TIMEOUT_REDIS_S (Section 06e's 500 ms) here as well as
            # by the client's socket timeouts: those bound one socket operation,
            # not a connect plus a command, and a client that retries multiplies
            # them. This await sits between two SSE frames.
            await asyncio.wait_for(pipe.execute(), timeout=settings.TIMEOUT_REDIS_S)
        except Exception:
            # Replay store write failure must never break the SSE stream, and
            # must not be paid for again on the next frame.
            self._redis_failed = True
            logger.warning(
                "EventStamper.push_to_redis: failed to persist event "
                "answer_run_id=%s event_name=%s event_seq=%d -- replay "
                "buffer disabled for the rest of this stream",
                self.answer_run_id,
                event_name,
                enriched.get("event_seq", -1),
                exc_info=True,
            )

    async def alias_to(self, redis: Any, persisted_run_id: str) -> None:
        """Copy this stream's ring buffer under the persisted run id.

        The buffer is keyed by the streaming-session UUID because that is
        the only id that exists before the silver.answer_runs INSERT. The
        replay endpoint, though, authorises and reads by the PERSISTED id,
        so without this copy it returned [] for every real run (CHAT-8 /
        API-2). Called once, on the ``completed`` frame, after which no
        further frames are written for this stream.

        Best-effort like :meth:`push_to_redis`: replay is a recovery aid and
        must never break the stream.
        """
        # Nothing was written once a write failed, so there is nothing to alias.
        if redis is None or self._redis_failed:
            return
        src = f"{_KEY_PREFIX}:{self.answer_run_id}"
        dst = f"{_KEY_PREFIX}:{persisted_run_id}"
        try:
            async with asyncio.timeout(settings.TIMEOUT_REDIS_S):
                await redis.copy(src, dst, replace=True)
                await redis.expire(dst, _TTL_S)
        except Exception:
            self._redis_failed = True
            logger.warning(
                "EventStamper.alias_to: failed to alias replay buffer "
                "stream_id=%s answer_run_id=%s",
                self.answer_run_id,
                persisted_run_id,
                exc_info=True,
            )
