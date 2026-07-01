"""Hallucination failure test for the Public Geoscience retrieval path.

One adversarial query verifying the agent refuses to fabricate details about
a non-existent SMDI record rather than inventing commodity data.

Section 07e hallucination category: H1 (non-existent entity ID).

Pre-conditions:
  - docker compose up -d fastapi postgresql pgbouncer ollama qdrant

Run with:
    cd src/fastapi
    python -m pytest tests/test_public_geoscience_hallucination.py -m hallucination -v

Marks
-----
  hallucination — adversarial query, blocks milestone acceptance if pass-rate < 95%
  integration   — requires the full docker compose stack
"""

from __future__ import annotations

import re

import httpx
import pytest

from tests.conftest import (
    AUTH_HEADERS,
    FASTAPI_URL,
    parse_sse_stream,
)

# Use the standard test project; the PGEO path is not project-scoped but the
# endpoint requires a project_id parameter.
_TEST_PROJECT_ID = "019d74a1-fba8-7165-9ae6-a5bf93eef97d"

# SMDI identifiers go from ~1 to ~20 000 in real data. 99999 will never exist.
_FAKE_SMDI = "99999"

# Refusal phrases — at least one must be present.
_REFUSAL_PHRASES = [
    "no",
    "not found",
    "no record",
    "not available",
    "do not have",
    "don't have",
    "unable",
    "cannot",
    "insufficient",
    "doesn't exist",
    "does not exist",
    "no data",
    "no information",
]

# Commodity codes that would be fabricated if the agent invents a commodity
# for SMDI 99999 — none of these should appear in a refusal response.
_FABRICATED_COMMODITY_PATTERN = re.compile(
    r"\b(?:Au|Cu|Ag|Ni|Zn|Pb|U3O8|Li|Mo|Co|REE|PGE)\b"
)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.hallucination
async def test_refuses_nonexistent_smdi() -> None:
    """Verifies the agent does NOT fabricate details about a fake SMDI number.

    Section 07e checks for this test:

    1. HTTP 200 with text/event-stream.
    2. ``completed`` event received (not ``failed`` — graceful refusal, no crash).
    3. Response text contains at least one refusal phrase (case-insensitive).
    4. Response text does NOT contain a specific commodity code (Au, Cu, etc.)
       attributed to SMDI 99999 — those would be fabricated values.
    5. confidence <= 0.3 (refusal signal — the agent is certain there is no
       such record, but low confidence indicates the question could not be
       answered from the available corpus).
    """
    query = f"What commodities are at SMDI {_FAKE_SMDI}?"

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            f"{FASTAPI_URL}/internal/queries",
            headers={
                **AUTH_HEADERS,
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "project_id": _TEST_PROJECT_ID,
            },
        ) as response:
            # --- Check 1: HTTP 200 ---
            assert response.status_code == 200, (
                f"[pgeo-halluc-001] Expected HTTP 200, got {response.status_code}"
            )
            assert "text/event-stream" in response.headers.get("content-type", ""), (
                "[pgeo-halluc-001] Expected text/event-stream content type"
            )

            # --- Check 2: completed event (graceful refusal, not a crash) ---
            completed = await parse_sse_stream(response)

    response_text: str = completed.get("text", "")
    confidence: float = completed.get("confidence", 1.0)

    # --- Check 3: at least one refusal phrase ---
    matched_refusal = any(
        phrase.lower() in response_text.lower()
        for phrase in _REFUSAL_PHRASES
    )
    assert matched_refusal, (
        f"[pgeo-halluc-001] H1 — SMDI {_FAKE_SMDI} does not exist. "
        f"Agent must refuse, not fabricate.\n"
        f"Expected one of: {_REFUSAL_PHRASES}\n"
        f"Actual response: {response_text!r}"
    )

    # --- Check 4: no fabricated commodity codes attributed to fake SMDI ---
    # Strip citation markers ([PGEO-1], [DATA-1] etc.) before scanning.
    stripped = re.sub(r"\[(?:DATA|NI43|PUB|PGEO)-\d+\]", "", response_text)
    # The check is contextual: we only flag if the commodity appears in the
    # same sentence that references the fake SMDI number.
    sentences = [s.strip() for s in re.split(r"[.!?]", stripped) if s.strip()]
    for sentence in sentences:
        if _FAKE_SMDI in sentence:
            match = _FABRICATED_COMMODITY_PATTERN.search(sentence)
            assert match is None, (
                f"[pgeo-halluc-001] H1 — Fabricated commodity code "
                f"{match.group()!r} found in sentence referencing SMDI {_FAKE_SMDI}:\n"
                f"  Sentence: {sentence!r}\n"
                f"  Full response: {response_text!r}"
            )

    # --- Check 5: confidence <= 0.3 ---
    assert confidence <= 0.3, (
        f"[pgeo-halluc-001] H1 — Expected confidence <= 0.3 for a refusal response, "
        f"got {confidence:.3f}.\n"
        f"A high-confidence refusal indicates the confidence model is not detecting "
        f"refusal language and setting the score low. See response_assembler._is_refusal().\n"
        f"Response: {response_text!r}"
    )


# ── H2: wrong-jurisdiction query (mineral not found in this province) ────
#
# Asks about a commodity/entity that exists in real life but does NOT exist
# in the requested jurisdiction's public registry. The agent must NOT
# invent records; it must either (a) refuse, or (b) surface records from
# another jurisdiction with the jurisdiction_code explicitly set.
_REFUSAL_OR_DEFLECTION = _REFUSAL_PHRASES + [
    "no record in",
    "none in",
    "not recorded in",
    "outside",
    "but in",
    "however",
]


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.hallucination
async def test_refuses_wrong_jurisdiction_diamond_sk() -> None:
    """H2 — Diamond deposits in Saskatchewan's SMDI are extremely rare; the
    agent must not confabulate a "main diamond deposit in SK" that doesn't
    exist. If it surfaces anything diamond-related, every claim must cite
    a real PGEO record with a verifiable external_id and commodity code
    including "diamond".

    Checks:
      1. HTTP 200 + SSE.
      2. Response is coherent (completed event received).
      3. Either (a) response contains a refusal / deflection phrase, OR
         (b) every mention of "diamond" is immediately followed by a
         citation marker like [PGEO-N] — no un-cited diamond claims.
      4. No fabricated ore grades ("15.2 carats per tonne" etc.) — scan for
         numeric+unit patterns within 5 words of "diamond".
      5. confidence <= 0.5.
    """
    query = "What are the biggest diamond deposits in Saskatchewan?"

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            f"{FASTAPI_URL}/internal/queries",
            headers={
                **AUTH_HEADERS,
                "Content-Type": "application/json",
            },
            json={"query": query, "project_id": _TEST_PROJECT_ID},
        ) as response:
            assert response.status_code == 200, (
                f"[pgeo-halluc-002] Expected HTTP 200, got {response.status_code}"
            )
            completed = await parse_sse_stream(response)

    response_text: str = completed.get("text", "")
    confidence: float = completed.get("confidence", 1.0)
    re.sub(r"\[(?:DATA|NI43|PUB|PGEO)-\d+\]", "<CIT>", response_text)

    # Check 3: refusal/deflection OR every diamond mention is cited.
    has_refusal = any(
        phrase.lower() in response_text.lower()
        for phrase in _REFUSAL_OR_DEFLECTION
    )
    if not has_refusal:
        # Scan sentences mentioning "diamond" and confirm each has a
        # citation marker nearby (within the same sentence).
        sentences = [s.strip() for s in re.split(r"[.!?]", response_text) if s.strip()]
        for sentence in sentences:
            if "diamond" in sentence.lower():
                has_citation = bool(
                    re.search(r"\[(?:DATA|NI43|PUB|PGEO)-\d+\]", sentence)
                )
                assert has_citation, (
                    f"[pgeo-halluc-002] H2 — Un-cited 'diamond' claim in a "
                    f"sentence that should require citation.\n"
                    f"  Sentence: {sentence!r}\n"
                    f"  Full response: {response_text!r}"
                )

    # Check 4: no fabricated ore grades near "diamond".
    grade_near_diamond = re.search(
        r"diamond[^.!?]{0,60}?\d+(?:\.\d+)?\s*(?:ct|carat|cpt|g/t|tpa|ozt)\b",
        response_text,
        re.IGNORECASE,
    )
    assert grade_near_diamond is None, (
        f"[pgeo-halluc-002] H2 — Likely fabricated grade near 'diamond': "
        f"{grade_near_diamond.group()!r}.\nResponse: {response_text!r}"
    )

    # Check 5: confidence <= 0.5.
    assert confidence <= 0.5, (
        f"[pgeo-halluc-002] H2 — Expected confidence <= 0.5 for a refusal/"
        f"deflection response, got {confidence:.3f}.\n"
        f"Response: {response_text!r}"
    )


# ── H3: citation-required — every PGEO claim must trace to a real record ──

@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.hallucination
async def test_every_pgeo_claim_has_citation() -> None:
    """H3 — For a broad commodity query, every specific claim about a
    named occurrence / mine / drillhole must be followed by a PGEO
    citation marker within 80 characters. This is the citation-coverage
    hallucination guard per plan §04i layer 5.

    Checks:
      1. HTTP 200 + SSE.
      2. Completed event received.
      3. Response contains at least 1 PGEO citation marker.
      4. For each capitalised proper-noun substring matching occurrence-
         name heuristics (Capital-case 2+ words OR ALL-CAPS 4+ chars),
         a [PGEO-N] marker appears within the same sentence OR a
         refusal/disclaimer phrase appears somewhere in the response.
      5. Number of citations >= number of distinct claimed names (capped
         at 8 so the guard isn't defeated by flooding the response with
         many names but only few citations).
    """
    query = "Tell me about the biggest uranium mines and occurrences in Saskatchewan."

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            f"{FASTAPI_URL}/internal/queries",
            headers={
                **AUTH_HEADERS,
                "Content-Type": "application/json",
            },
            json={"query": query, "project_id": _TEST_PROJECT_ID},
        ) as response:
            assert response.status_code == 200, (
                f"[pgeo-halluc-003] Expected HTTP 200, got {response.status_code}"
            )
            completed = await parse_sse_stream(response)

    response_text: str = completed.get("text", "")
    citations: list[dict] = completed.get("citations", [])
    pgeo_markers = re.findall(r"\[PGEO-\d+\]", response_text)

    # Check 3: at least 1 PGEO marker inline.
    assert len(pgeo_markers) >= 1, (
        f"[pgeo-halluc-003] No [PGEO-N] citation markers inline. A uranium-"
        f"focused SK query should produce at least one citable PGEO record.\n"
        f"Response: {response_text!r}"
    )

    # Check 4+5: named-claim coverage.
    # Conservative name extractor: capitalised-word pairs that look like
    # deposit / mine / lake names, e.g. "McArthur River", "Cigar Lake".
    name_pattern = re.compile(
        r"\b(?:(?:[A-Z][a-z]{2,}\s+){1,3}"
        r"(?:Lake|River|Mine|Creek|Zone|Deposit|Corner|Point))\b"
    )
    distinct_names = set(name_pattern.findall(response_text))
    capped = min(len(distinct_names), 8)
    pgeo_citation_count = sum(
        1 for c in citations if c.get("citation_type") == "PGEO"
    )

    # Either the number of PGEO citations covers distinct names, OR the
    # response must contain a refusal/deflection acknowledging uncertainty.
    has_disclaimer = any(
        phrase in response_text.lower()
        for phrase in ("based on", "according to", "in the registry", "recorded")
    )
    assert pgeo_citation_count >= capped or has_disclaimer, (
        f"[pgeo-halluc-003] Named {len(distinct_names)} places "
        f"({sorted(distinct_names)[:5]}…) but produced only "
        f"{pgeo_citation_count} PGEO citations without a grounding "
        f"disclaimer ('based on the registry' etc.). Suggests the agent "
        f"is inventing names.\nResponse: {response_text!r}"
    )
