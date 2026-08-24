"""The identifier boost cannot reach the answer, and this pins why.

WHY THIS FILE EXISTS
    ``identifier_boost`` detects a hole ID in a query and widens the SPARSE
    prefetch from 100 to 150 candidates. The module used to say this let
    "more exact-token candidates enter the cross-store RRF pool", the logs
    say the boost fired, and the ADR narrative says identifier queries are
    being helped.

    None of it reaches the answer. Reciprocal Rank Fusion scores a
    candidate at 1/(k + rank). The fused output is RETRIEVAL_TOP_N = 40
    long and the DENSE branch alone supplies 100 candidates, so the top 40
    are already filled by terms from ranks 0-39. Every slot the boost adds
    sits at rank 100-149 and scores strictly lower. A chunk that only the
    sparse branch finds, at rank 137, cannot get in -- and that is exactly
    the chunk someone asking about PLS-22-08 wants.

    The failure mode this guards is not "the boost is dead". It is
    "somebody changed PREFETCH_LIMIT or RETRIEVAL_TOP_N and the boost
    quietly came alive, or quietly died, on the answer path, with no test
    noticing either way".

WHY THE ASSERTIONS DO NOT HARDCODE QDRANT'S k
    Qdrant's RRF uses k = 2, but the inequality that matters holds for
    EVERY non-negative k: 1/(k + 39) > 1/(k + 100) always. Depending on
    the constant would make this test wrong the day Qdrant changed it,
    while the conclusion would still be right.

WHAT WOULD ACTUALLY FIX IT
    A third Prefetch doing MatchText on the detected token, whose rank 0
    scores 1/(k+0) -- an order of magnitude above anything the dense branch
    produces. It is not implemented here because MatchText needs a
    full-text payload index on `text` that nothing in the live tree
    creates; see the module docstring for the precondition to check first.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.identifier_boost import (
    SPARSE_BOOST_FACTOR,
    detect_identifiers,
)
from app.services.qdrant_service import PREFETCH_LIMIT


def rrf(rank: int, k: float = 2.0) -> float:
    """Reciprocal Rank Fusion term for a 0-indexed rank."""
    return 1.0 / (k + rank)


def boosted_sparse_limit() -> int:
    """What hybrid_query computes for a boosted sparse prefetch."""
    return max(1, int(PREFETCH_LIMIT * SPARSE_BOOST_FACTOR))


class TestDetectionItselfWorks:
    """Detection is not the broken part, and saying so keeps the next
    person from 'fixing' the regexes."""

    @pytest.mark.parametrize(
        ("query", "token"),
        [
            ("what are the results for PLS-22-08?", "PLS-22-08"),
            ("assays from 23-MS-117", "23-MS-117"),
            ("geology of 74I12", "74I12"),
        ],
    )
    def test_an_identifier_query_fires_and_captures_the_token(
        self, query: str, token: str,
    ) -> None:
        result = detect_identifiers(query)
        assert result.has_match
        assert token in result.matched_tokens
        assert result.boost_factor == SPARSE_BOOST_FACTOR

    def test_a_plain_question_does_not_fire(self) -> None:
        result = detect_identifiers("how many drill holes are in the project?")
        assert result.has_match is False
        assert result.boost_factor == 1.0

    def test_the_matched_token_is_available_to_the_caller(self) -> None:
        """The real fix needs the token, not just the factor.

        nodes.py reads only ``.boost_factor`` today. The token is already
        captured, so wiring a MatchText branch is a plumbing change, not a
        detection change.
        """
        assert detect_identifiers("PLS-22-08 assays").matched_tokens == [
            "PLS-22-08"]


class TestTheBoostCannotReachTheOutput:
    def test_the_boost_really_does_widen_the_sparse_prefetch(self) -> None:
        """The premise. If this fails, the rest of the file is about
        something that no longer happens."""
        assert boosted_sparse_limit() > PREFETCH_LIMIT

    def test_the_dense_branch_alone_fills_the_fused_output(self) -> None:
        """This is the whole reason the extra slots are unreachable."""
        assert PREFETCH_LIMIT >= settings.RETRIEVAL_TOP_N, (
            f"dense prefetch {PREFETCH_LIMIT} no longer covers "
            f"RETRIEVAL_TOP_N {settings.RETRIEVAL_TOP_N} -- the boost may "
            f"now be able to contribute, which would be a REAL change to "
            f"the answer path. Re-derive the arithmetic in "
            f"identifier_boost's docstring before accepting this."
        )

    @pytest.mark.parametrize("k", [0.0, 1.0, 2.0, 60.0])
    def test_every_added_slot_scores_below_the_last_kept_dense_slot(
        self, k: float,
    ) -> None:
        """Holds for any k, so it does not depend on Qdrant's constant."""
        last_kept_dense = rrf(settings.RETRIEVAL_TOP_N - 1, k)
        best_added_slot = rrf(PREFETCH_LIMIT, k)

        assert best_added_slot < last_kept_dense, (
            f"at k={k} the first boosted slot ({best_added_slot:.5f}) now "
            f"outranks the 40th dense candidate ({last_kept_dense:.5f})"
        )

    def test_the_gap_is_wide_not_marginal(self) -> None:
        """Recorded so a small constant change does not silently close it.

        At Qdrant's k=2 the weakest kept dense candidate scores about 2.5x
        the best slot the boost adds. That is a structural gap, not a
        rounding one.
        """
        ratio = rrf(settings.RETRIEVAL_TOP_N - 1) / rrf(PREFETCH_LIMIT)
        assert ratio > 2.0, (
            f"the margin has narrowed to {ratio:.2f}x -- the boost is close "
            f"to becoming live, which is a behaviour change on the answer "
            f"path and wants a benchmark, not a constant bump"
        )

    def test_the_worst_added_slot_is_worse_still(self) -> None:
        assert rrf(boosted_sparse_limit() - 1) < rrf(PREFETCH_LIMIT)


class TestTheDocstringStillSaysSo:
    """The module's own text is the thing most likely to be believed.

    It claimed the opposite of the arithmetic for long enough that an ADR
    and a log line were written on top of it. If someone reverts the
    correction without changing the code, that is worth catching.
    """

    def test_the_module_documents_its_own_inertness(self) -> None:
        from app.services import identifier_boost

        doc = identifier_boost.__doc__ or ""
        assert "why it currently changes nothing" in doc
        assert "MatchText" in doc, (
            "the docstring should still name the fix and its precondition"
        )

    def test_it_does_not_still_claim_the_pool_helps(self) -> None:
        from app.services import identifier_boost

        doc = identifier_boost.__doc__ or ""
        stale = "more exact-token candidates enter the cross-store RRF pool"
        # The corrected text quotes the old claim to explain it, so the
        # check is that it is not presented as current -- i.e. it appears
        # only inside the paragraph that retracts it.
        if stale in doc:
            retraction = doc.split(stale)[0]
            assert "used to end by saying" in retraction, (
                "the old claim is back as a statement of fact"
            )
