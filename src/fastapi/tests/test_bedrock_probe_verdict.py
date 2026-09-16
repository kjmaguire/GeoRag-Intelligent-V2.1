"""The Bedrock probe must not report a pass over a report that verifies nothing.

ADR-0022 makes the probe's committed report the gate on trusting any Bedrock
adapter. Every section of that probe degrades instead of raising — deliberately,
so one dead section does not cost you the others — which means a run that fails
completely still writes a well-formed JSON file.

Before 2026-09-08 the script then printed "COMMIT THIS REPORT" and exited 0 over
a report whose every section was a 403. Running it with no AWS credentials is
what surfaced that: a file would have landed on disk, satisfying the gate by its
existence while containing no evidence at all.

That is the same shape as the other defects this migration turned up — not an
error, just something quietly not carrying the information it claims to.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ops" / "validation"))

from bedrock_probe import _EVIDENCE_SECTIONS, verdict  # noqa: E402


def _report(**overrides) -> dict:
    """A full report with every evidence section present, then overridden."""
    base = {name: {"skipped": "unset"} for name in _EVIDENCE_SECTIONS}
    base.update(overrides)
    return base


def test_a_fully_observed_run_passes() -> None:
    v = verdict(_report(**{n: {"result": "ok"} for n in _EVIDENCE_SECTIONS}))
    assert v["verified_anything"]
    assert not v["sections_failed"]
    assert not v["sections_missing"]


def test_one_real_observation_is_enough_to_be_evidence() -> None:
    """A throttle on one model does not invalidate what the others showed."""
    v = verdict(_report(chat={"result": "ok"},
                        embed={"error": {"code": "ThrottlingException"}}))
    assert v["verified_anything"]
    assert v["sections_failed"] == ["embed"]
    assert not v["authentication_failed"]


@pytest.mark.parametrize("code", [
    "UnrecognizedClientException",
    "InvalidClientTokenId",
    "AccessDeniedException",
    "ExpiredTokenException",
])
def test_an_auth_failure_everywhere_is_not_a_pass(code: str) -> None:
    v = verdict(_report(**{n: {"error": {"code": code}} for n in _EVIDENCE_SECTIONS}))
    assert not v["verified_anything"], "a report of nothing but auth errors is not evidence"
    assert v["authentication_failed"]
    assert "authenticate" in v["summary"]


def test_auth_failure_is_caught_when_only_availability_saw_it() -> None:
    """The sections can all be *skipped* (no model ids) while auth is the cause."""
    v = verdict(_report(availability={
        "cohere_serverless_error": {"code": "InvalidClientTokenId"}
    }))
    assert not v["verified_anything"]
    assert v["authentication_failed"]


def test_everything_skipped_is_not_a_pass() -> None:
    """Unset model ids are a configuration problem, not a clean bill of health."""
    v = verdict(_report())
    assert not v["verified_anything"]
    assert not v["authentication_failed"]


def test_a_missing_section_is_not_a_pass() -> None:
    """Absence must not read as success.

    A section absent from the report has neither "error" nor "skipped", so a
    naive check counts it as observed. That is how adding a name to
    _EVIDENCE_SECTIONS without wiring it up would inflate the verified count.
    """
    v = verdict({})
    assert not v["verified_anything"]
    assert v["sections_missing"] == list(_EVIDENCE_SECTIONS)
    assert not v["sections_ok"]
