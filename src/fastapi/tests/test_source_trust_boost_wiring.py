"""Pin the audit item D contract — source-trust boost wiring.

Background
----------
``app/services/source_trust/boost.py`` shipped fully-implemented in an
earlier phase but had ZERO callers — verified by grep at audit time. The
boost-by-trust formula was dead weight. Item D wires it into
``search_documents`` post-rerank, behind ``SOURCE_TRUST_BOOST_ENABLED``
with a shadow-mode rollout (compute + log without mutating scores) so
the rerank delta can be measured against the golden bench BEFORE
flipping to live.

These tests pin:

  1. The four config knobs exist with the documented defaults.
  2. The metrics module exports the boost counter + rank-delta histogram.
  3. ``search_documents`` actually imports + calls ``boost_by_trust``
     guarded on the flag.
  4. The shadow-vs-live branch is present so the rollout discipline
     stays in the source.
  5. The post-boost write path clips to [0, 1] (Citation contract).

Pattern matches the other tenancy / observability regression tests in
this repo: file-content + import-time assertions so the test runs
without a live DB. The runtime behaviour gets exercised in the
agentic-retrieval golden-question suite once the flag flips on.
"""
from __future__ import annotations

from pathlib import Path


TOOLS_PY = Path(__file__).resolve().parents[1] / "app" / "agent" / "tools.py"


def test_settings_defaults_match_rollout_plan() -> None:
    """ENABLED defaults OFF; SHADOW defaults ON for safe first-flip."""
    from app.config import settings

    # Default: boost is OFF — nothing changes on import / first deploy.
    assert settings.SOURCE_TRUST_BOOST_ENABLED is False, (
        "SOURCE_TRUST_BOOST_ENABLED must default False. Flipping the "
        "default in a follow-up change is fine, but only after the "
        "shadow-mode bench has been reviewed (AUDIT_AND_FIX_REPORT.md "
        "item D rollout step 3)."
    )
    # When ENABLED flips on, SHADOW defaults TRUE so the first turn-on
    # is observation-only. Same pattern as WORKSPACE_RESOLUTION_FAILURES
    # phased rollout (item B).
    assert settings.SOURCE_TRUST_BOOST_SHADOW_MODE is True, (
        "SOURCE_TRUST_BOOST_SHADOW_MODE must default True. The whole "
        "point of the phased rollout is that flipping ENABLED first "
        "lights up the counter WITHOUT mutating live retrieval."
    )
    # Weight default mirrors the original §12.8 boost formula default
    # baked into boost.py. Changing this is fine but cross-check the
    # formula commentary in tools.py.
    assert settings.SOURCE_TRUST_BOOST_WEIGHT == 0.2
    # Fallback 0.5 = neutral trust (modulation formula evaluates to 1.0
    # at trust=0.5, so missing scores are pass-through).
    assert settings.SOURCE_TRUST_BOOST_FALLBACK == 0.5


def test_metrics_export_boost_counters() -> None:
    """The Prometheus surfaces must exist before the wire site references them."""
    from app.metrics import (
        SOURCE_TRUST_BOOST_APPLIED,
        SOURCE_TRUST_BOOST_RANK_DELTA,
    )

    # Counter label cardinality is the headline contract — Grafana
    # panels and alerts pivot on these label names. Drifting them
    # without updating the dashboard JSON breaks the rollout review.
    sample = SOURCE_TRUST_BOOST_APPLIED.labels(mode="shadow", top1_changed="false")
    sample.inc(0)  # smoke — labels valid
    SOURCE_TRUST_BOOST_RANK_DELTA.observe(0)  # smoke — histogram callable


def test_search_documents_imports_boost_helper() -> None:
    """tools.py must reference boost_by_trust + the flag at the wire seam."""
    src = TOOLS_PY.read_text(encoding="utf-8")
    assert "boost_by_trust" in src, (
        "search_documents must call boost_by_trust. Without this wire "
        "the boost helper is dead code — exactly the state audit item D "
        "was opened to fix."
    )
    assert "SOURCE_TRUST_BOOST_ENABLED" in src, (
        "Wire site must be guarded on the feature flag. Unconditional "
        "calls would defeat the phased rollout."
    )
    assert "SOURCE_TRUST_BOOST_SHADOW_MODE" in src, (
        "Wire site must branch on SHADOW_MODE so shadow turn-on does "
        "NOT mutate the live ordering."
    )


def test_wire_site_handles_shadow_vs_live_branches() -> None:
    """Both branches must be present + the live branch must mutate scores."""
    src = TOOLS_PY.read_text(encoding="utf-8")
    # Live branch must actually update relevance_score AND re-sort, or
    # the boost will be a no-op even with SHADOW_MODE=False.
    # Tolerate whitespace/newlines between the call pieces (pint may
    # reflow); the load-bearing facts are (a) we touch relevance_score
    # and (b) the value is clipped to [0, 1].
    import re as _re

    clip_pattern = _re.compile(
        r"c\.relevance_score\s*=\s*max\(\s*0\.0\s*,\s*min\(\s*1\.0",
        _re.DOTALL,
    )
    assert clip_pattern.search(src), (
        "Live branch must clip the boosted score to [0, 1]. The Citation "
        "model declares relevance_score as a constrained float in that "
        "range; an out-of-range value would trip Pydantic validation "
        "downstream and turn what looked like a ranking improvement into "
        "a hard 422."
    )
    assert "reordered.sort(" in src, (
        "Live branch must re-sort by boosted score. Without re-sorting "
        "the boost only changes the float on each chunk, not the order — "
        "context_prep / response_assembler iterate chunks in list order, "
        "so the boost effectively wouldn't fire."
    )
    # Shadow path must call the metric labelled mode="shadow" + must
    # NOT execute the reorder. The simplest pin: the reorder is wrapped
    # in the `if not ... SHADOW_MODE:` branch.
    assert "if not settings.SOURCE_TRUST_BOOST_SHADOW_MODE:" in src, (
        "Shadow vs live MUST be selected on SHADOW_MODE inside the wire "
        "site, not on ENABLED. ENABLED toggles whether the boost runs at "
        "all; SHADOW_MODE toggles whether the run mutates ordering."
    )


def test_wire_site_failure_is_best_effort() -> None:
    """A boost failure must NOT break live retrieval."""
    src = TOOLS_PY.read_text(encoding="utf-8")
    # The whole boost block must be wrapped in try/except so the
    # retrieval call doesn't fail closed on (e.g.) a missing
    # silver.source_trust_scores table. Pin the explicit error log so
    # rollout still has observability if the wrap swallows everything.
    assert "source-trust boost failed" in src, (
        "Boost block must log on failure — silent swallow would hide "
        "integration bugs during the shadow rollout, defeating the "
        "whole point of running shadow mode first."
    )


def test_boost_helper_signature_still_matches_call_site() -> None:
    """If boost_by_trust grows / loses kwargs, the tools.py call breaks."""
    import inspect

    from app.services.source_trust import boost_by_trust

    sig = inspect.signature(boost_by_trust)
    # The wire site passes these kwargs by name; drift here = TypeError
    # the second the boost flag flips on in prod.
    expected = {
        "workspace_id",
        "retrieved_chunks",
        "boost_weight",
        "fallback_trust",
    }
    actual = set(sig.parameters.keys())
    missing = expected - actual
    assert not missing, (
        f"boost_by_trust signature drift — wire site in tools.py passes "
        f"kwargs {expected} but helper only accepts {actual}. Missing: "
        f"{missing}. Either update the helper or the wire site, but the "
        f"shadow rollout WILL trip on first call otherwise."
    )
