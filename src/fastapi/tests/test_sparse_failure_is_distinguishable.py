"""A dead sparse leg must not look like an empty corpus.

Global Invariant 11 says a sparse-encoder failure must fail the query
rather than degrade to a dense-only one. Half of that held: asyncio.gather
raises on the first failing leg, so no answer is ever built from a
dense-only query pretending to be hybrid.

The other half did not. search_documents catches the exception and returns
an empty DocumentSearchResult, and every consumer downstream reads that as
"nothing matched". A comment above the gather claimed the opposite -- "the
outer wait_for propagates the exception" -- which is what made it easy to
believe the invariant was enforced end to end.

That matters on this deployment specifically. `sparse` runs desired=1 on
Fargate Spot, so a reclamation produces this window as routine rather than
as an outage, and SPLADE++ has no hosted equivalent on any cloud, so there
is nothing to fail over to. Knowing is the whole mitigation: what the
sparse leg matches -- hole IDs, sample numbers, NTS codes -- is exactly
what a dense vector is worst at, so its absence reads as "the platform does
not know about DDH-22-14" rather than as an outage.
"""

from __future__ import annotations

import pytest

from app.services.sparse_encoder import SparseEncoderUnavailable


def test_the_exception_type_exists_and_is_catchable() -> None:
    assert issubclass(SparseEncoderUnavailable, Exception)
    with pytest.raises(SparseEncoderUnavailable):
        raise SparseEncoderUnavailable("sidecar refused the connection")


def test_search_documents_separates_the_two_failures() -> None:
    """The two except branches must produce different data_source strings.

    Read from source rather than executed: reaching search_documents' error
    branches for real needs a Qdrant client, an embedding backend and an
    event loop. What is asserted here is the property that was missing --
    that the branches are distinguishable at all.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "app" / "agent" / "tools.py"
    text = source.read_text(encoding="utf-8")

    assert "SparseEncoderUnavailable" in text, (
        "search_documents must recognise a sparse-leg failure specifically"
    )
    assert "sparse encoder unavailable" in text, (
        "the sparse branch must set a data_source a human can tell apart from "
        "a generic error and from an empty result"
    )
    assert "SPARSE_ENCODER_UNAVAILABLE" in text, (
        "the marker CloudWatch alarms on must be emitted here"
    )

    # The three outcomes must be three different strings. If two collapse,
    # the alarm still fires but the operator cannot tell from the answer's
    # own provenance which one happened.
    for distinct in ("(timeout)", "(error)", "(sparse encoder unavailable)"):
        assert distinct in text, f"missing the {distinct} outcome"


def test_the_marker_has_an_alarm_watching_for_it() -> None:
    """An emitter with no alarm is a log line nobody reads."""
    from pathlib import Path

    alerts = (
        Path(__file__).resolve().parents[3]
        / "deploy"
        / "aws"
        / "terraform"
        / "alerts.tf"
    )
    if not alerts.is_file():  # pragma: no cover - deploy tree absent
        pytest.skip("deploy/aws/terraform not present in this checkout")

    text = alerts.read_text(encoding="utf-8")
    assert "SPARSE_ENCODER_UNAVAILABLE" in text
    # It is emitted by the fastapi service, so it must be filtered on the
    # services log group -- the sweep group cannot contain it. This is the
    # mistake that left the Sev 1 Bedrock alarm unable to fire for weeks.
    block = text[text.index("sparse-encoder-unavailable") :][:1200]
    assert 'log_group   = "services"' in block


def test_the_gather_comment_no_longer_claims_propagation() -> None:
    """The comment asserted a guarantee the code below cancels.

    Left as a test rather than just fixed, because a future edit that
    restores the swallow-everything branch would want to restore this
    sentence with it.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "app" / "agent" / "tools.py"
    text = source.read_text(encoding="utf-8")
    assert "the outer wait_for propagates" not in text, (
        "search_documents catches the exception and returns an empty result; "
        "it does not propagate out of the function"
    )
