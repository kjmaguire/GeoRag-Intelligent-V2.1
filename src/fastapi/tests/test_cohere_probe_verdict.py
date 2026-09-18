"""The Cohere probe must not report a pass over a report that verifies nothing.

The sibling of `test_bedrock_probe_verdict.py`, for the probe ADR-0023 added.
The reasoning is the same and is not repeated here — read that file, or
`ops/validation/_probe_verdict.py`, for why a well-formed JSON file full of
auth failures once satisfied a gate by existing.

What IS specific to this probe, and is why the file is not just a copy:

- Failures arrive as **HTTP statuses**, not botocore error codes, so
  "could not authenticate" is recognised differently. A probe that silently
  stopped recognising it would still exit 1 on an empty run — but it would
  tell an operator "no section produced an observation" when the true answer
  is "your key is wrong", and those send you to different files.
- There IS a credential here, unlike on Bedrock, and the report is meant to
  be committed. `_redact` is the thing standing between a provider echoing
  request material back in an error and that material landing in git. This
  repository has already committed one live credential.
- The last test is an anti-fork ratchet. Both probes now share one
  implementation of "an absent section is not a pass"; two copies is how one
  of them loses it, and the copy that loses it is the one nobody reads.
"""

from __future__ import annotations

import string
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ops" / "validation"))

from cohere_probe import _EVIDENCE_SECTIONS, _redact, verdict  # noqa: E402


def _report(**overrides) -> dict:
    """A full report with every evidence section present, then overridden."""
    report = {name: {"observed": True} for name in _EVIDENCE_SECTIONS}
    report["reachability"] = {"key_length": 40}
    report.update(overrides)
    return report


class TestTheVerdictRefusesToPassNothing:
    def test_a_fully_observed_run_passes(self) -> None:
        v = verdict(_report())
        assert v["verified_anything"] is True
        assert v["sections_ok"] == list(_EVIDENCE_SECTIONS)
        assert v["sections_missing"] == []

    def test_an_all_error_run_verifies_nothing(self) -> None:
        v = verdict(_report(**{n: {"error": {"type": "X"}} for n in _EVIDENCE_SECTIONS}))
        assert v["verified_anything"] is False
        assert v["sections_failed"] == list(_EVIDENCE_SECTIONS)

    def test_an_all_skipped_run_verifies_nothing(self) -> None:
        """The shape of running with no key at all: every section skips, and
        the JSON file that lands is perfectly well-formed."""
        v = verdict(_report(**{n: {"skipped": "COHERE_API_KEY unset"} for n in _EVIDENCE_SECTIONS}))
        assert v["verified_anything"] is False
        assert v["sections_skipped"] == list(_EVIDENCE_SECTIONS)

    def test_an_absent_section_is_missing_not_passed(self) -> None:
        """Absence-as-success, which is the whole shape being guarded against.

        This is how adding a name to _EVIDENCE_SECTIONS without wiring up the
        section that produces it would quietly inflate the verified count.
        """
        report = _report()
        del report["parse"]
        v = verdict(report)
        assert "parse" in v["sections_missing"]
        assert "parse" not in v["sections_ok"]
        assert "MISSING" in v["summary"]

    def test_one_good_section_is_enough_to_be_partial_not_failed(self) -> None:
        report = _report(**{n: {"error": {"type": "X"}} for n in _EVIDENCE_SECTIONS})
        report["chat"] = {"observed": True}
        v = verdict(report)
        assert v["verified_anything"] is True
        assert v["sections_failed"] and v["sections_ok"] == ["chat"]


class TestAuthenticationIsNamedSeparately:
    """ "Your key is wrong" and "the adapter is wrong" send you to different
    files, so the verdict has to tell them apart."""

    def test_a_401_across_the_board_is_reported_as_an_auth_failure(self) -> None:
        v = verdict(
            _report(**{n: {"error": {"status": 401, "code": "AuthenticationError"}} for n in _EVIDENCE_SECTIONS})
        )
        assert v["authentication_failed"] is True
        assert "COHERE_API_KEY" in v["summary"]

    def test_reachability_alone_can_report_the_auth_failure(self) -> None:
        """It is the FIRST call a run makes, so it is where a wrong key shows
        up before anything else has had a chance to."""
        report = _report(**{n: {"skipped": "x"} for n in _EVIDENCE_SECTIONS})
        report["reachability"] = {"error": {"status": 403, "code": "AuthenticationError"}}
        v = verdict(report)
        assert v["authentication_failed"] is True
        assert v["verified_anything"] is False

    def test_an_ordinary_failure_is_not_an_auth_failure(self) -> None:
        """A 404 on the model name is a real finding, and calling it an auth
        problem would send the operator to rotate a key that is fine."""
        v = verdict(_report(**{n: {"error": {"status": 404, "code": "NotFound"}} for n in _EVIDENCE_SECTIONS}))
        assert v["authentication_failed"] is False
        assert "no section produced an observation" in v["summary"]


class TestTheReportIsSafeToCommit:
    """Unlike the Bedrock probe there is a credential here, and the report is
    meant to land in git. This repository has already committed one live
    credential (`scripts/phase0_acceptance.sh`), which is still in history."""

    def test_key_shaped_strings_are_redacted(self) -> None:
        # The fake key is BUILT, not written out, and that is not fussiness.
        # scripts/check-no-committed-secrets.php scans every tracked file for
        # exactly this shape — a long mixed-case alphanumeric run with no
        # separators — and a literal here fails that gate. Which is the gate
        # working: a test for a redactor needs a string indistinguishable from
        # what it redacts, so the two checks are in genuine tension.
        #
        # Composing it resolves the tension without spending an exemption. The
        # alternative was adding this file to the scanner's SKIP_PATHS, which
        # would exempt it forever — including from a real key pasted here
        # later by someone who never read this comment.
        fake_key = string.ascii_letters[:26] + string.digits
        assert len(fake_key) >= 24, "must stay long enough to look like a key"

        leaked = f"invalid api token: {fake_key}"
        assert fake_key not in _redact(leaked)
        assert "<redacted>" in _redact(leaked)

    def test_ordinary_prose_survives(self) -> None:
        """A redactor that eats the error message costs you the finding."""
        assert _redact("model not found") == "model not found"

    def test_short_identifiers_survive(self) -> None:
        """Model names are the thing an operator most needs to read back."""
        assert _redact("parse-v5.0 is not enabled") == "parse-v5.0 is not enabled"


def test_both_probes_share_one_implementation() -> None:
    """Anti-fork ratchet.

    "An absent section is not a pass" is the single most important line in
    either probe, and it exists once. If someone reimplements it in one of
    them, that copy is free to drift — and the one that drifts is the one
    nobody is reading, because the reason to read it only arrives on the day
    it matters.
    """
    import bedrock_probe
    import cohere_probe
    from _probe_verdict import compute_verdict

    assert bedrock_probe.compute_verdict is compute_verdict
    assert cohere_probe.compute_verdict is compute_verdict


@pytest.mark.parametrize("probe_module", ["bedrock_probe", "cohere_probe"])
def test_neither_probe_passes_a_report_with_no_sections_at_all(probe_module: str) -> None:
    """The degenerate case, held for both: an empty dict is well-formed JSON."""
    import importlib

    module = importlib.import_module(probe_module)
    v = module.verdict({})
    assert v["verified_anything"] is False
    assert v["sections_missing"] == list(module._EVIDENCE_SECTIONS)


@pytest.mark.parametrize("probe_module", ["bedrock_probe", "cohere_probe"])
def test_a_section_whose_every_call_failed_is_not_an_observation(probe_module: str) -> None:
    """The absence-as-success shape, one level down. Live until 2026-09-15.

    `probe_chat` fans out over request variants and puts each outcome on a
    nested dict, so the section itself carries no `error` key:

        {"model": "...", "with_response_format": {"error": ...}, ...}

    A key check on the section counted that as an observation, so a run where
    every single call was a 401 reported "verified 1/4 sections" — and the
    file it wrote is the one ADR-0022 and ADR-0023 treat as the gate on
    trusting the adapters.

    Found by running the Cohere probe against a fake server that 401s
    everything, not by reading either probe. Both share the fix.
    """
    import importlib

    module = importlib.import_module(probe_module)
    section = {
        "model": "some-model",
        "without_response_format": {"error": {"code": "X"}},
        "with_response_format": {"error": {"code": "X"}},
    }
    report = {name: {"observed": True} for name in module._EVIDENCE_SECTIONS}
    report["chat"] = section

    v = module.verdict(report)
    assert "chat" in v["sections_failed"]
    assert "chat" not in v["sections_ok"]


@pytest.mark.parametrize("probe_module", ["bedrock_probe", "cohere_probe"])
def test_a_section_with_one_good_call_among_failures_still_counts(probe_module: str) -> None:
    """The other direction, which matters just as much.

    Marking a section failed because ONE variant errored would throw away
    the observation the run actually made — and the whole design of these
    probes is that one dead section must not cost you the others.
    """
    import importlib

    module = importlib.import_module(probe_module)
    report = {name: {"observed": True} for name in module._EVIDENCE_SECTIONS}
    report["chat"] = {
        "model": "some-model",
        "without_response_format": {"latency_s": 0.4, "text_head": "hi"},
        "with_response_format": {"error": {"code": "X"}},
    }

    v = module.verdict(report)
    assert "chat" in v["sections_ok"]


@pytest.mark.parametrize("probe_module", ["bedrock_probe", "cohere_probe"])
def test_a_section_whose_variants_are_grouped_is_still_judged(probe_module: str) -> None:
    """The same defect, one level deeper than the fix that created the module.

    `probe_chat` puts its variants directly on the section, so a one-level
    scan found them. `probe_parse` GROUPS its variants -- `formats` and
    `pixel_ladder` -- and neither group is itself a result or looks like
    one, so that scan found nothing, fell through to "ok", and reported a
    section in which every call had failed as verified.

    This is not hypothetical and not a fixture: it is the exact shape the
    2026-09-18 go-live rehearsal produced when the sandbox's egress proxy
    refused every request to api.cohere.com. The probe printed "verified
    1/4 sections (ok=parse)" and "COMMIT THIS REPORT" over a report whose
    every single call was a 403 -- flipping the one instruction that
    protects ADR-0023's evidence gate.
    """
    import importlib

    module = importlib.import_module(probe_module)
    report = {name: {"observed": True} for name in module._EVIDENCE_SECTIONS}
    report["parse"] = {
        "model": "parse-v5.0",
        "formats": {
            "blocks": {"error": {"type": "ProxyError", "message": "403 Forbidden"}},
            "markdown": {"error": {"type": "ProxyError", "message": "403 Forbidden"}},
        },
        "pixel_ladder": {
            "1900000": {"error": {"type": "ProxyError", "message": "403 Forbidden"}},
        },
    }

    v = module.verdict(report)
    assert "parse" in v["sections_failed"]
    assert "parse" not in v["sections_ok"]


@pytest.mark.parametrize("probe_module", ["bedrock_probe", "cohere_probe"])
def test_a_grouped_section_with_a_real_observation_still_passes(probe_module: str) -> None:
    """Recursing must not overshoot into marking healthy sections failed."""
    import importlib

    module = importlib.import_module(probe_module)
    report = {name: {"observed": True} for name in module._EVIDENCE_SECTIONS}
    report["parse"] = {
        "model": "parse-v5.0",
        "formats": {
            "blocks": {"page0_keys": ["text", "bbox"], "status": 200},
            "markdown": {"error": {"type": "ProxyError", "message": "403 Forbidden"}},
        },
    }

    v = module.verdict(report)
    assert "parse" in v["sections_ok"]


@pytest.mark.parametrize("probe_module", ["bedrock_probe", "cohere_probe"])
def test_a_section_carrying_only_config_is_not_mistaken_for_failure(
    probe_module: str,
) -> None:
    """A nested dict that recorded no call at all must stay neutral.

    The conservative half of `_looks_like_a_result`, asserted now that the
    scan recurses: being wrong here would mark a healthy section failed and
    block a deploy on a metadata block.
    """
    import importlib

    module = importlib.import_module(probe_module)
    report = {name: {"observed": True} for name in module._EVIDENCE_SECTIONS}
    report["parse"] = {
        "model": "parse-v5.0",
        "settings": {"limits": {"max_pixels": 1900000}},
        "observed": True,
    }

    v = module.verdict(report)
    assert "parse" in v["sections_ok"]
