"""AL-10: an exhausted pool cannot hang the post-assembly guards (§04i, §06e).

Layer 4's hole-ID lookup and Layer 5's provenance enrichment both bounded the
FETCH with ``TIMEOUT_POSTGIS_S`` and left ``pool.acquire()`` -- the call that
actually waits when every connection is checked out -- outside the bound. With
the pool exhausted, ``validate_node`` waited on a connection forever and the
user's stream never completed.

Each fixture below is a pool whose ``acquire()`` context never yields a
connection. The guards must give up after ``TIMEOUT_POSTGIS_S`` and keep their
existing posture: Layer 4 fails CLOSED (the critical "could not complete"
warning, so a fabricated hole cannot ride a pool outage), Layer 5 stays soft
(the answer ships without the provenance line).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from app.agent.hallucination.layer5_provenance import enrich_provenance
from app.agent.hallucination.orchestrator_validators import verify_entities
from app.models.rag import Citation, GeoRAGResponse

REPORT_ID = "3f1c2a9e-8b7d-4c61-9a0e-2d5b7f4e1c08"
PROJECT_ID = "5e2b8c1d-7a4f-4e39-b6d0-91c3a8f2e7b4"

#: The test's own backstop: well above the 0.05 s guard timeout, far below a
#: hang. A guard that does not bound ``acquire`` trips THIS and fails the test
#: instead of hanging the run.
_BACKSTOP_S = 3.0


class _ExhaustedPool:
    """``acquire()`` returns a context manager that waits for a connection
    that never comes -- every connection is checked out."""

    def __init__(self) -> None:
        self.acquire_attempts = 0
        self.connections_used = 0

    def acquire(self) -> Any:
        pool = self
        pool.acquire_attempts += 1

        class _Acquire:
            async def __aenter__(self) -> Any:
                await asyncio.Event().wait()  # never set
                pool.connections_used += 1
                raise AssertionError("unreachable")

            async def __aexit__(self, *_exc: object) -> bool:
                return False

        return _Acquire()


class TestLayer4HoleLookupIsBoundedOnAcquire:
    @pytest.mark.asyncio
    async def test_an_exhausted_pool_fails_closed_instead_of_hanging(self) -> None:
        pool = _ExhaustedPool()
        with patch("app.agent.hallucination.orchestrator_validators.settings") as ms:
            ms.ENTITY_RESOLUTION_ENABLED = True
            ms.TIMEOUT_POSTGIS_S = 0.05
            ms.TIMEOUT_NEO4J_S = 3.0
            warnings = await asyncio.wait_for(
                verify_entities(
                    "Drill hole PLS-20-01 was completed. [DATA-1]",
                    PROJECT_ID,
                    pool,
                    None,
                    tool_results=[],
                ),
                timeout=_BACKSTOP_S,
            )

        assert pool.acquire_attempts == 1 and pool.connections_used == 0
        # The critical prefix is what makes run_post_assembly_validation retry
        # and floor the answer: the hole could not be ruled out.
        assert any(w.startswith("Layer 4: Drill-hole ID resolution could not complete") for w in warnings), warnings

    @pytest.mark.asyncio
    async def test_the_timeout_bounds_acquire_and_fetch_together(self) -> None:
        """One TIMEOUT_POSTGIS_S for the whole lookup, not one per step: a pool
        that hands out a connection after 0.04 s and a fetch that takes 0.04 s
        is over a 0.05 s budget."""

        class _SlowConn:
            async def fetch(self, *_a: object, **_k: object) -> list[dict[str, str]]:
                await asyncio.sleep(0.04)
                return [{"hole_id": "PLS-20-01"}]

        class _SlowPool:
            def acquire(self) -> Any:
                class _Acquire:
                    async def __aenter__(self) -> Any:
                        await asyncio.sleep(0.04)
                        return _SlowConn()

                    async def __aexit__(self, *_exc: object) -> bool:
                        return False

                return _Acquire()

        with patch("app.agent.hallucination.orchestrator_validators.settings") as ms:
            ms.ENTITY_RESOLUTION_ENABLED = True
            ms.TIMEOUT_POSTGIS_S = 0.05
            ms.TIMEOUT_NEO4J_S = 3.0
            warnings = await asyncio.wait_for(
                verify_entities(
                    "Drill hole PLS-20-01 was completed. [DATA-1]",
                    PROJECT_ID,
                    _SlowPool(),
                    None,
                    tool_results=[],
                ),
                timeout=_BACKSTOP_S,
            )
        assert any(w.startswith("Layer 4: Drill-hole ID resolution could not complete") for w in warnings)

    @pytest.mark.asyncio
    async def test_a_healthy_pool_is_untouched(self) -> None:
        class _Conn:
            async def fetch(self, *_a: object, **_k: object) -> list[dict[str, str]]:
                return [{"hole_id": "PLS-20-01"}]

        class _Pool:
            def acquire(self) -> Any:
                class _Acquire:
                    async def __aenter__(self) -> Any:
                        return _Conn()

                    async def __aexit__(self, *_exc: object) -> bool:
                        return False

                return _Acquire()

        with patch("app.agent.hallucination.orchestrator_validators.settings") as ms:
            ms.ENTITY_RESOLUTION_ENABLED = True
            ms.TIMEOUT_POSTGIS_S = 0.05
            ms.TIMEOUT_NEO4J_S = 3.0
            warnings = await verify_entities(
                "Drill hole PLS-20-01 was completed. [DATA-1]",
                PROJECT_ID,
                _Pool(),
                None,
                tool_results=[],
            )
        assert not any(w.startswith("Layer 4: Drill-hole ID") for w in warnings), warnings


class TestLayer5EnrichmentIsBoundedOnAcquire:
    @pytest.mark.asyncio
    async def test_an_exhausted_pool_ships_the_answer_without_provenance(self) -> None:
        citation = Citation(
            citation_id="[NI43-1]",
            citation_type="NI43",
            source_chunk_id=f"georag_reports:{REPORT_ID}:section=14.2:chunk=chunk-1",
            document_title="NI 43-101 Technical Report",
            relevance_score=0.8,
        )
        response = GeoRAGResponse(
            text="The deposit hosts 8.6 Mt [NI43-1].",
            citations=[citation],
            confidence=0.8,
            sources_used=[citation.source_chunk_id],
        )
        pool = _ExhaustedPool()
        with patch("app.agent.hallucination.layer5_provenance.settings") as ms:
            ms.TIMEOUT_POSTGIS_S = 0.05
            out = await asyncio.wait_for(enrich_provenance(response, pool), timeout=_BACKSTOP_S)

        assert pool.acquire_attempts == 1 and pool.connections_used == 0
        # Soft: the answer and its citation survive; only the provenance line
        # (an enrichment, not a guard) is missing.
        assert out is response
        assert out.citations[0].provenance is None
        assert out.text == "The deposit hosts 8.6 Mt [NI43-1]."
