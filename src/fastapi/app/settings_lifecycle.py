"""Settings lifecycle scaffolding (REC#4 — 2026-06-03).

This is a SCAFFOLD, not the complete migration. The full delivery is
a multi-day effort (173 settings in ``config.py``, each needs a
classification + reasoning). This module provides the type machinery +
the FIRST 10 categorised settings so the pattern is established and
visible.

Background
----------
Audit item H (2026-06-03) found that the prior audit's "22 dead
settings" estimate was overcounted (real number was 2) because the
original sweep grepped only ``settings.NAME`` and missed ``getattr(
settings, "NAME", default)`` readers. The real architectural problem
isn't the dead settings — it's that NO setting has a declared
LIFECYCLE STAGE, so audits have to re-derive it from scratch every
time.

This module fixes that by giving each setting a tagged
:class:`SettingLifecycle` enum value:

  - ``PROPOSED``     — declared but no consumer wired yet
  - ``SHADOW``       — wired, observe-only metric, no behaviour change
  - ``LIVE``         — wired + active
  - ``DEPRECATED``   — wired but slated for removal; readers should
                       migrate to the replacement before next release
  - ``DEAD``         — declared, no consumer; tag-or-delete decision
                       pending

CI gate (future work)
---------------------
Once the registry is fully populated, a CI check enforces:
  - PROPOSED settings must graduate to SHADOW within 30 days of
    introduction (forces wiring follow-through)
  - SHADOW settings must graduate to LIVE within 90 days of clean
    metric data (prevents indefinite shadow-mode parking)
  - DEAD settings raise a build warning until removed
  - DEPRECATED settings require a `replaced_by` pointer
  - Adding a new setting without a lifecycle entry is a hard error

The registry below is the SEED. Operators add entries as they wire /
sunset / observe each setting. The audit pattern then becomes
"reconcile registry vs config.py" rather than "re-derive everything."

REC#4 implementation status
---------------------------
Phase 1 (this commit): type machinery + seed entries for the 10
settings touched by the 2026-06-03 audits. The remaining 163 entries
are deferred to subsequent sessions — adding them in bulk would be a
"week of focused work" item.

Phase 2 (follow-up): bulk-categorise the remaining settings + a CI
check that fails if config.py has a setting not in the registry.

Phase 3: time-based graduation enforcement (PROPOSED→SHADOW after
30d, etc.).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class SettingLifecycle(StrEnum):
    """Lifecycle stage of a setting. Strings so the registry remains
    grep-friendly + serialisable."""

    PROPOSED = "proposed"          # declared, no consumer wired yet
    SHADOW = "shadow"              # wired, observe-only metric
    LIVE = "live"                  # wired + active
    DEPRECATED = "deprecated"      # slated for removal, readers should migrate
    DEAD = "dead"                  # no consumer, tag-or-delete pending


@dataclass(frozen=True)
class SettingEntry:
    """Registry entry for one setting.

    Fields
    ------
    name: The setting field name on ``app.config.Settings``.
    stage: Current lifecycle stage.
    owner: Code path / module that "owns" the setting (where its
        primary consumer lives). Used for routing audit findings.
    since: ISO date string when this entry was last updated. Used by
        the future time-based graduation gate.
    observe_metric: Prometheus metric name that confirms SHADOW-stage
        observability. Required for SHADOW; ignored for other stages.
    graduates_to: For SHADOW stage, the target stage (almost always
        LIVE) so the gate knows what graduation looks like.
    replaced_by: For DEPRECATED stage, the canonical replacement
        (another setting name, a function name, etc.).
    notes: Free-form context. Memory-link `[[memory-name]]` allowed.
    """

    name: str
    stage: SettingLifecycle
    owner: str
    since: date
    observe_metric: str | None = None
    graduates_to: SettingLifecycle | None = None
    replaced_by: str | None = None
    notes: str = ""
    # Inferred fields for the future CI gate.
    requires_observability: bool = field(init=False)

    def __post_init__(self) -> None:
        # SHADOW must have an observability metric — otherwise it's not
        # "shadow", it's just "off" with extra steps. Bypass __setattr__
        # because the dataclass is frozen.
        requires_obs = self.stage == SettingLifecycle.SHADOW
        object.__setattr__(self, "requires_observability", requires_obs)


# ─── Registry seed ──────────────────────────────────────────────────
#
# This is the FIRST 10 entries — the settings touched by the
# 2026-06-03 audits. Subsequent sessions extend this. Order matches
# config.py top-to-bottom so manual reconciliation against the source
# file is easy.

REGISTRY: dict[str, SettingEntry] = {
    "SOURCE_TRUST_BOOST_ENABLED": SettingEntry(
        name="SOURCE_TRUST_BOOST_ENABLED",
        stage=SettingLifecycle.SHADOW,
        owner="app/agent/tools.py:search_documents",
        since=date(2026, 6, 3),
        observe_metric="georag_source_trust_boost_applied_total",
        graduates_to=SettingLifecycle.LIVE,
        notes=(
            "Audit item D. Wired post-rerank, gated by ENABLED. Flip to "
            "LIVE after one sprint of clean shadow-mode metric data + "
            "golden-bench parity. SHADOW_MODE controls mutate-vs-observe; "
            "when ENABLED=False the whole branch is skipped."
        ),
    ),
    "SOURCE_TRUST_BOOST_SHADOW_MODE": SettingEntry(
        name="SOURCE_TRUST_BOOST_SHADOW_MODE",
        stage=SettingLifecycle.SHADOW,
        owner="app/agent/tools.py:search_documents",
        since=date(2026, 6, 3),
        observe_metric="georag_source_trust_boost_applied_total",
        graduates_to=SettingLifecycle.LIVE,
        notes="Companion to SOURCE_TRUST_BOOST_ENABLED. See item D notes.",
    ),
    "SOURCE_TRUST_BOOST_WEIGHT": SettingEntry(
        name="SOURCE_TRUST_BOOST_WEIGHT",
        stage=SettingLifecycle.LIVE,
        owner="app/services/source_trust/boost.py",
        since=date(2026, 6, 3),
        notes="Multiplier in the boost formula; tuneable per environment.",
    ),
    "SOURCE_TRUST_BOOST_FALLBACK": SettingEntry(
        name="SOURCE_TRUST_BOOST_FALLBACK",
        stage=SettingLifecycle.LIVE,
        owner="app/services/source_trust/boost.py",
        since=date(2026, 6, 3),
        notes="Neutral trust value for sources missing from silver.source_trust_scores.",
    ),
    "SUMMARIZER_ENABLED": SettingEntry(
        name="SUMMARIZER_ENABLED",
        stage=SettingLifecycle.DEAD,
        owner="app/services/corpus_summarizer.py (no consumer)",
        since=date(2026, 6, 3),
        notes=(
            "Audit item H. Declared as a gate but NO code reads it. "
            "corpus_summarizer runs unconditionally when summarize_scope() "
            "is called directly; orchestrator never calls it because the "
            "IntentRoute.SUMMARIZE dispatch was never added. Wire-or-delete."
        ),
    ),
    "TEMPERATURE_BY_QUERY_TYPE": SettingEntry(
        name="TEMPERATURE_BY_QUERY_TYPE",
        stage=SettingLifecycle.DEAD,
        owner="config.py only (no consumer)",
        since=date(2026, 6, 3),
        notes=(
            "Audit item H. ClassVar dict added per OVERNIGHT_LOG step 2b "
            "but never imported. VLLM_TEMPERATURE is the single applied "
            "global temperature. Wire-or-delete."
        ),
    ),
    "LLM_FALLBACK_ENABLED": SettingEntry(
        name="LLM_FALLBACK_ENABLED",
        stage=SettingLifecycle.DEAD,
        owner="config.py only (tagged in P2-D pass)",
        since=date(2026, 6, 3),
        notes=(
            "Tagged in earlier audit (AUDIT_AND_FIX_REPORT.md P2-D). "
            "Companion to LLM_FALLBACK_URL/MODEL/API_KEY — none have "
            "production consumers."
        ),
    ),
    "RATE_LIMIT_ENABLED": SettingEntry(
        name="RATE_LIMIT_ENABLED",
        stage=SettingLifecycle.DEPRECATED,
        owner="app/services/rate_limit.py",
        since=date(2026, 6, 3),
        replaced_by="Laravel-side throttle:* middleware (audit item F)",
        notes=(
            "FastAPI-side rate-limit module exists but is superseded by "
            "the Laravel edge throttle (audit item F shipped per-workspace "
            "'uploads' and 'charts' buckets). FastAPI module readers should "
            "migrate; this entry tracks the deprecation."
        ),
    ),
    "MULTI_QUERY_EXPANSION_ENABLED": SettingEntry(
        name="MULTI_QUERY_EXPANSION_ENABLED",
        stage=SettingLifecycle.LIVE,
        owner="app/agent/tools.py + app/services/multi_query_expansion.py",
        since=date(2026, 6, 1),
        notes="Default ON since 2026-06-01 retrieval quality overhaul.",
    ),
    "SOURCE_TRUST_BOOST_FALLBACK_": SettingEntry(  # placeholder for the spec
        name="SOURCE_TRUST_BOOST_FALLBACK_",
        stage=SettingLifecycle.PROPOSED,
        owner="(none yet — placeholder demonstrating PROPOSED stage)",
        since=date(2026, 6, 3),
        notes=(
            "Placeholder example showing the PROPOSED → SHADOW → LIVE flow. "
            "Replace with a real PROPOSED entry when one comes along, or "
            "remove during cleanup."
        ),
    ),
}


def get_entry(name: str) -> SettingEntry | None:
    """Lookup a registry entry by setting name. None if not registered."""
    return REGISTRY.get(name)


def entries_by_stage(stage: SettingLifecycle) -> list[SettingEntry]:
    """All registered settings at a given stage. Useful for ops queries
    ('show me everything in SHADOW') and the future CI gate."""
    return [e for e in REGISTRY.values() if e.stage == stage]


__all__ = [
    "REGISTRY",
    "SettingEntry",
    "SettingLifecycle",
    "entries_by_stage",
    "get_entry",
]
