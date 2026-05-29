"""Plan §I — shadow-telemetry Sentry tag-setters.

Implements the contract in
``docs/architecture/shadow_telemetry_sentry_tags.md``: every shadow
spine stamps a small, fixed-cardinality set of Sentry tags on the
current scope so the Sentry filter UI lists clean axes
(``repair.terminal_strategy``, ``context_prep.budget_pressure``, …)
instead of 40 near-duplicate engineer-invented keys.

Five stamping functions, one per spine call site:

  • ``stamp_repair_tags``        — ``repair_shadow_node`` (Stage 1) +
                                    the future ``repair_loop_node`` share
                                    the same tag shape.
  • ``stamp_context_prep_tags``  — ``assemble_node`` after
                                    ``prepare_evidence_for_intent``.
  • ``stamp_multi_turn_tags``    — ``resolve_node`` after
                                    ``resolve_multi_turn``.
  • ``stamp_evidence_tags`` +
    ``stamp_guards_tags``        — ``persist_node`` after
                                    ``classify_guards`` (the latest
                                    upstream point with all data in hand).

Each function is **best-effort**: a missing ``sentry_sdk`` import or
a bad tag value MUST NOT block the answer path. This mirrors the
``repair_shadow_node`` defensive-logging pattern. The SDK was removed
2026-05-21 and re-enabled 2026-05-27; the lazy import keeps this
module safe to load even when the SDK is missing again.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings


logger = logging.getLogger(__name__)


__all__ = [
    "stamp_repair_tags",
    "stamp_context_prep_tags",
    "stamp_multi_turn_tags",
    "stamp_evidence_tags",
    "stamp_guards_tags",
    "stamp_workspace_tag",
    "stamp_card_type_tag",
]


# ---------------------------------------------------------------------------
# Internal helpers — lazy SDK + normalisation
# ---------------------------------------------------------------------------


def _sentry_sdk() -> Any | None:
    """Lazy-import sentry_sdk; None when the SDK isn't installed.

    Each call site uses ``sdk = _sentry_sdk(); if sdk is None: return``
    so the cost of a missing SDK is one import attempt per request,
    not a hard fail.
    """
    try:
        import sentry_sdk  # noqa: PLC0415 — lazy on purpose
    except ImportError:
        return None
    return sentry_sdk


def _bool_str(value: Any) -> str:
    """Normalise a bool to ``"true"`` / ``"false"``.

    Per the spec's normalisation rules — never ``"True"`` / ``"1"``,
    because Sentry's filter UI treats them as distinct values.
    """
    return "true" if bool(value) else "false"


def _confidence_bucket(value: float | None) -> str:
    """Bucket a 0-1 confidence into one of four enum values.

    Cardinality matters: a numeric ``multi_turn.confidence=0.873`` tag
    blows out Sentry's tag UI within a week. Bucketed enums let the
    dashboard stay readable.
    """
    if value is None:
        return "unknown"
    if value >= 0.85:
        return "high"
    if value >= 0.60:
        return "medium"
    return "low"


def _budget_pressure_bucket(packet: Any) -> str:
    """Bucket EvidencePacket budget into ``comfortable`` / ``tight`` / ``over``.

    Same bucket scheme as the Grafana panel — keeping the two in sync
    means a Sentry filter and a Grafana legend always speak the same
    language. Threshold mirrors plan §3f.
    """
    if packet is None:
        return "unknown"
    total = getattr(packet, "total_tokens", None)
    remaining = getattr(packet, "remaining_budget", None)
    if total is None or remaining is None:
        return "unknown"
    if remaining < 0:
        return "over"
    # Treat <=10% of total as tight (i.e. ≥90% used).
    if total > 0 and remaining <= max(int(total * 0.10), 256):
        return "tight"
    return "comfortable"


# ---------------------------------------------------------------------------
# Workspace tag — applies to every transaction, not spine-specific
# ---------------------------------------------------------------------------


def stamp_workspace_tag(workspace_id: Any) -> None:
    """Stamp the ``workspace.id`` tag.

    Called from the entry function so every span on the current scope
    carries it — Grafana / Sentry filters consistently AND on workspace.
    """
    sdk = _sentry_sdk()
    if sdk is None:
        return
    try:
        if workspace_id is not None:
            sdk.set_tag("workspace.id", str(workspace_id))
    except Exception:
        logger.debug("sentry workspace tag set failed (non-fatal)", exc_info=True)


# ---------------------------------------------------------------------------
# repair.* — §4b/§4c shadow + full loop share this shape
# ---------------------------------------------------------------------------


def stamp_repair_tags(state: Any, plan: Any | None) -> None:
    """Stamp the ``repair.*`` tags.

    Reads `state.repair_codes_observed`, `state.repair_attempts`,
    `state.repair_terminal_reason` + the dispatched ``RepairPlan`` for
    ``terminal_strategy``. Plan can be None when the shadow planner
    didn't fire (e.g. no guard codes).
    """
    sdk = _sentry_sdk()
    if sdk is None:
        return
    try:
        sdk.set_tag(
            "repair.shadow_mode",
            _bool_str(settings.REPAIR_LOOP_SHADOW_ENABLED),
        )

        codes = getattr(state, "repair_codes_observed", []) or []
        sdk.set_tag("repair.codes_count", str(len(codes)))

        terminal = bool(plan and getattr(plan, "terminal", False))
        sdk.set_tag("repair.terminal", _bool_str(terminal))

        terminal_strategy = ""
        if terminal and plan is not None:
            strat = getattr(plan, "strategy", None)
            # RepairStrategy enum → .value, plain str passes through.
            terminal_strategy = getattr(strat, "value", strat) or ""
        sdk.set_tag("repair.terminal_strategy", str(terminal_strategy))

        attempts = getattr(state, "repair_attempts", []) or []
        sdk.set_tag("repair.attempts", str(len(attempts)))

        # Death-loop is observable from repair_terminal_reason carrying
        # the death-loop sentinel string written by detect_death_loop;
        # see plan §4b. Cheap heuristic that doesn't need the loop
        # driver to thread an extra bool through state.
        reason = getattr(state, "repair_terminal_reason", "") or ""
        sdk.set_tag(
            "repair.death_loop",
            _bool_str("death_loop" in reason.lower()),
        )
    except Exception:
        logger.debug("sentry repair tag set failed (non-fatal)", exc_info=True)


# ---------------------------------------------------------------------------
# context_prep.* — §3 composition
# ---------------------------------------------------------------------------


def stamp_context_prep_tags(state: Any, prepared: Any | None) -> None:
    """Stamp the ``context_prep.*`` tags.

    ``prepared`` is a :class:`PreparedContext` (frozen dataclass from
    :mod:`app.agent.context_prep`) or None when the flag is off / the
    prep step was skipped.
    """
    sdk = _sentry_sdk()
    if sdk is None:
        return
    try:
        sdk.set_tag(
            "context_prep.enabled",
            _bool_str(settings.CONTEXT_PREP_ENABLED),
        )
        intent = getattr(state, "effective_intent", None) or getattr(state, "intent", None)
        intent_value = getattr(intent, "value", intent) or "unspecified"
        sdk.set_tag("context_prep.intent", str(intent_value))

        if prepared is not None:
            reached = bool(getattr(prepared, "reached_budget", False))
            sdk.set_tag("context_prep.budget_reached", _bool_str(reached))

            drops = getattr(prepared, "dropped_evidence_ids", []) or []
            sdk.set_tag("context_prep.drops_count", str(len(drops)))

            packet = getattr(prepared, "packet", None)
            sdk.set_tag(
                "context_prep.budget_pressure",
                _budget_pressure_bucket(packet),
            )
        else:
            # Prep didn't fire — flat distinct tag values so filters
            # don't break.
            sdk.set_tag("context_prep.budget_reached", "false")
            sdk.set_tag("context_prep.drops_count", "0")
            sdk.set_tag("context_prep.budget_pressure", "unknown")
    except Exception:
        logger.debug("sentry context_prep tag set failed (non-fatal)", exc_info=True)


# ---------------------------------------------------------------------------
# multi_turn.* — §3e
# ---------------------------------------------------------------------------


def stamp_multi_turn_tags(state: Any) -> None:
    """Stamp the ``multi_turn.*`` tags.

    Reads ``state.resolution_trace`` + ``state.resolution_confidence``
    + ``state.history`` (length only). All optional; defaults keep the
    tag shape stable so the dashboard never sees a missing tag.
    """
    sdk = _sentry_sdk()
    if sdk is None:
        return
    try:
        sdk.set_tag(
            "multi_turn.enabled",
            _bool_str(settings.MULTI_TURN_RESOLUTION_ENABLED),
        )
        trace = getattr(state, "resolution_trace", []) or []
        sdk.set_tag("multi_turn.made_changes", _bool_str(len(trace) > 0))
        sdk.set_tag("multi_turn.steps_count", str(len(trace)))

        confidence = getattr(state, "resolution_confidence", None)
        sdk.set_tag(
            "multi_turn.confidence_bucket",
            _confidence_bucket(confidence),
        )

        history = getattr(state, "history", []) or []
        sdk.set_tag("multi_turn.history_depth", str(len(history)))
    except Exception:
        logger.debug("sentry multi_turn tag set failed (non-fatal)", exc_info=True)


# ---------------------------------------------------------------------------
# evidence.* + guards.* — persist_node site
# ---------------------------------------------------------------------------


# Set of GuardErrorCode.value strings that map to a TERMINAL repair
# strategy. Kept inline (not imported) so the tag-setter doesn't fail
# when the repair planner module is shuffled around; the contract is
# in repair_loop_spec.md §4. Update both this set and the dispatcher
# table together.
_TERMINAL_GUARD_CODES: frozenset[str] = frozenset({
    "CONFLICTING_SOURCES",         # → SURFACE_CONFLICT
    "STALE_DOCUMENT",              # → SURFACE_STALE
    "AMBIGUOUS_ENTITY",            # → ASK_FOR_DISAMBIGUATION
    "NO_EVIDENCE",                 # → ABSTAIN_NO_EVIDENCE
    "POLICY_REFUSAL",              # → ABSTAIN_POLICY
})


def stamp_evidence_tags(packet: Any | None) -> None:
    """Stamp the ``evidence.*`` tags.

    Reads kind counts off the prepared :class:`EvidencePacket`. Falls
    back to flat zeros + empty strings when the packet is None so the
    tag shape stays stable.
    """
    sdk = _sentry_sdk()
    if sdk is None:
        return
    try:
        if packet is None:
            sdk.set_tag("evidence.kinds_count", "0")
            sdk.set_tag("evidence.has_spatial", "false")
            sdk.set_tag("evidence.has_graph", "false")
            sdk.set_tag("evidence.first_kind", "")
            return

        evidence = getattr(packet, "evidence", []) or []
        # Each typed evidence carries a `kind` discriminator (Pydantic
        # discriminated union). Defensive fallback: __class__.__name__.
        kinds_present: list[str] = []
        for e in evidence:
            kind = getattr(e, "kind", None) or e.__class__.__name__
            kinds_present.append(str(kind))

        distinct_kinds = set(kinds_present)
        sdk.set_tag("evidence.kinds_count", str(len(distinct_kinds)))
        sdk.set_tag(
            "evidence.has_spatial",
            _bool_str("spatial" in distinct_kinds or "SpatialEvidence" in distinct_kinds),
        )
        sdk.set_tag(
            "evidence.has_graph",
            _bool_str("graph" in distinct_kinds or "GraphEvidence" in distinct_kinds),
        )
        sdk.set_tag(
            "evidence.first_kind",
            kinds_present[0] if kinds_present else "",
        )
    except Exception:
        logger.debug("sentry evidence tag set failed (non-fatal)", exc_info=True)


def stamp_guards_tags(guard_failure_codes: list[str] | None) -> None:
    """Stamp the ``guards.*`` tags.

    Called with the same code list ``classify_guards`` returned to
    ``persist_node``. ``None`` and ``[]`` are both treated as the
    "no firings" baseline.
    """
    sdk = _sentry_sdk()
    if sdk is None:
        return
    try:
        codes = list(guard_failure_codes or [])
        sdk.set_tag("guards.fired_any", _bool_str(len(codes) > 0))

        fired_terminal = any(c in _TERMINAL_GUARD_CODES for c in codes)
        sdk.set_tag("guards.fired_terminal", _bool_str(fired_terminal))

        # Sentry caps tag value length at 200 chars. Truncate cleanly
        # at a code boundary rather than mid-token so the CSV is still
        # parseable in the Sentry UI.
        csv = ",".join(codes)
        if len(csv) > 200:
            kept = []
            length = 0
            for c in codes:
                # +1 for the joining comma except on the first entry.
                add = len(c) + (1 if kept else 0)
                if length + add > 197:  # leave room for "..." sentinel
                    break
                kept.append(c)
                length += add
            csv = ",".join(kept) + ("..." if len(kept) < len(codes) else "")
        sdk.set_tag("guards.codes_csv", csv)
    except Exception:
        logger.debug("sentry guards tag set failed (non-fatal)", exc_info=True)


# ---------------------------------------------------------------------------
# card.* — §6b P6 chat-card rendering observability
# ---------------------------------------------------------------------------

# Known `chart_type` values emitted by `_build_chat_card_payloads`. Listed
# here so the Sentry tag stays low-cardinality (Sentry's filter UI lists
# distinct values; an unbounded enum blows out the dropdown). New chart
# types added to the backend dispatcher should also land here.
_KNOWN_CARD_TYPES: frozenset[str] = frozenset({
    "drill_trace_3d",
    "downhole_strip",
    "stereonet",
    "technique_timeline",
    "coverage_table",
    "assay_histogram",
    "cross_section",
    "graph_viz",
})


def stamp_card_type_tag(viz_payload: Any | None) -> None:
    """Stamp the ``card.*`` tags from the agentic response's viz_payload.

    Two tags emitted (both always set so Sentry filters never see a
    missing key):

      * ``card.rendered`` (bool) — true iff viz_payload is non-None.
        Lets dashboards measure "what fraction of queries produce
        an inline visualisation."
      * ``card.type`` (enum) — one of ``_KNOWN_CARD_TYPES`` or
        ``"none"`` (no viz) or ``"unknown"`` (chart_type drifted from
        the known set — flag for spec-doc update).

    Per the §I spec normalisation rules: enum strings, no ``None``/None,
    no booleans-as-strings other than ``"true"``/``"false"``. The
    stamper is best-effort — SDK absent or attribute access failure
    silently no-ops.

    Called from ``persist_node`` alongside the other §I stampers, at
    the point where ``state.response`` is fully assembled.
    """
    sdk = _sentry_sdk()
    if sdk is None:
        return
    try:
        if viz_payload is None:
            sdk.set_tag("card.rendered", "false")
            sdk.set_tag("card.type", "none")
            return

        chart_type = getattr(viz_payload, "chart_type", None) or ""
        if chart_type in _KNOWN_CARD_TYPES:
            value = chart_type
        elif chart_type:
            value = "unknown"
        else:
            value = "none"

        sdk.set_tag("card.rendered", _bool_str(value not in ("none",)))
        sdk.set_tag("card.type", value)
    except Exception:
        logger.debug("sentry card_type tag set failed (non-fatal)", exc_info=True)
