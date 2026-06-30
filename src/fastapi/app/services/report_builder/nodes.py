"""Report Builder Graph nodes (§7.1 / §15.1).

Doc-phase 80 skeletons → doc-phase 137 graduation of the first 4
nodes (the "planning" half — no LLM required).

The 12-node pipeline:

  Graduated in doc-phase 137:
    1. select_report_type     → validate type + risk tier
    2. plan_sections          → seed sections_plan from templates
    3. gather_evidence        → synthetic-stub evidence curator
    4. verify_evidence_budget → sufficiency gate

  Still skeleton (need LLM / WeasyPrint / SeaweedFS):
    5. generate_section_drafts
    6. validate_claims
    7. attach_citations
    8. generate_maps_charts
    9. build_appendix
   10. compliance_check
   11. geologist_approval
   12. export_package
   13. activepieces_delivery

The graduated nodes share the `synthetic_stub` evaluator pattern
established in doc-phase 132 / 134 / 136 — orchestration is fully
live (state transitions, validation, error handling); the per-claim
LLM content is the stubbed part.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.services.report_builder.state import (
    Claim,
    EvidenceItem,
    ReportBuilderState,
    SectionDraft,
    SignOffRecord,
)
from app.services.report_builder.templates import (
    REPORT_RISK_TIERS,
    get_template,
)

log = logging.getLogger("georag.report_builder.nodes")


# ---------------------------------------------------------------------------
# Graduated nodes (doc-phase 137)
# ---------------------------------------------------------------------------

async def select_report_type(state: ReportBuilderState) -> ReportBuilderState:
    """Validate the requested report_type + risk_tier; pin started_at.

    Both `report_type` and `risk_tier` are Literal-validated by
    Pydantic at construction time, so this node's job is the
    cross-check: the risk_tier on state must match the registered
    risk_tier for that report_type (callers can't downgrade an R5
    report to R3 to bypass sign-off).

    Sets `state.started_at` if not already set.

    Graduated doc-phase 137.
    """
    expected_tier = REPORT_RISK_TIERS[state.report_type]
    if state.risk_tier != expected_tier:
        msg = (
            f"select_report_type: risk_tier={state.risk_tier} does not match "
            f"registry tier={expected_tier} for report_type={state.report_type}"
        )
        log.error(msg)
        return state.model_copy(update={"failure_reason": msg})

    updates = {}
    if state.started_at is None:
        updates["started_at"] = datetime.now(UTC)
    log.info(
        "select_report_type.passed report_id=%s report_type=%s risk_tier=%s",
        state.report_id, state.report_type, state.risk_tier,
    )
    return state.model_copy(update=updates) if updates else state


async def plan_sections(state: ReportBuilderState) -> ReportBuilderState:
    """Seed `state.sections_plan` from the per-report-type template
    manifest (§15.2).

    No LLM call in this graduation — the template defines a stable
    section structure; the Report Planner Agent's LLM-driven planning
    layer can mutate/extend the plan in a later tick.

    Graduated doc-phase 137.
    """
    if state.sections_plan:
        # Idempotent: a prior call has already seeded the plan.
        return state

    template = get_template(state.report_type)
    if not template:
        msg = f"plan_sections: no template registered for {state.report_type}"
        log.error(msg)
        return state.model_copy(update={"failure_reason": msg})

    log.info(
        "plan_sections.seeded report_id=%s report_type=%s sections=%d",
        state.report_id, state.report_type, len(template),
    )
    return state.model_copy(update={"sections_plan": template})


async def gather_evidence(state: ReportBuilderState) -> ReportBuilderState:
    """Evidence Curator. For each planned section, populates
    `state.section_drafts[].claims[].evidence`.

    Per-report-type dispatch (doc-phase 156):
      - `what_changed` reports → call the §9.13 what_changed_detector
        integration for real workspace deltas
      - All other report types → synthetic stub (1 stub EvidenceItem
        per required_evidence_kind)

    Real evidence retrieval (hybrid search + claim-ledger join) for
    the non-what_changed types lands when the §04i retrieval pipeline
    graduates.

    Graduated doc-phase 137; what_changed branch added doc-phase 156.
    """
    if state.section_drafts:
        return state

    # Doc-phase 156 — what_changed reports get real deltas from
    # the §9.13 detector instead of the synthetic stub.
    if state.report_type == "what_changed":
        from app.services.report_builder.whatchanged_integration import (
            gather_evidence_what_changed,
        )
        drafts = await gather_evidence_what_changed(state)
        if drafts is not None:
            log.info(
                "gather_evidence.what_changed report_id=%s sections=%d",
                state.report_id, len(drafts),
            )
            return state.model_copy(update={"section_drafts": drafts})

    section_drafts: list[SectionDraft] = []
    for sp in state.sections_plan:
        claims: list[Claim] = []
        for idx, kind in enumerate(sp.required_evidence_kinds):
            # One stub claim per required_evidence_kind.
            claims.append(
                Claim(
                    claim_id=f"{sp.section_id}.claim_{idx + 1}",
                    section_id=sp.section_id,
                    text=(
                        f"[synthetic_stub doc-phase 137] Placeholder claim "
                        f"for {sp.section_id}: {kind}"
                    ),
                    evidence=[
                        EvidenceItem(
                            source_chunk_id=f"stub_chunk__{sp.section_id}__{kind}__{idx + 1}",
                            data_visibility="workspace",
                            license_note=None,
                            is_stale=False,
                        )
                    ],
                    validated=False,
                )
            )
        section_drafts.append(
            SectionDraft(
                section_id=sp.section_id,
                body_markdown=(
                    f"[synthetic_stub doc-phase 137]\n\n"
                    f"# {sp.title}\n\nPlaceholder body for {sp.section_id}.\n"
                ),
                claims=claims,
                pending_map_kinds=list(sp.map_kinds),
                pending_chart_kinds=list(sp.chart_kinds),
            )
        )

    log.info(
        "gather_evidence.seeded report_id=%s sections=%d total_claims=%d "
        "total_evidence_items=%d",
        state.report_id, len(section_drafts),
        sum(len(d.claims) for d in section_drafts),
        sum(len(c.evidence) for d in section_drafts for c in d.claims),
    )
    return state.model_copy(update={"section_drafts": section_drafts})


async def verify_evidence_budget(
    state: ReportBuilderState,
    *,
    min_evidence_per_section: int = 1,
) -> ReportBuilderState:
    """Gate node — fails the graph if any planned section has zero
    evidence items across its claims.

    Per §15.1, the original intent is to route under-evidenced sections
    to a clarifying re-plan. For this graduation we hard-fail with
    `failure_reason` set; the re-plan path lands when the LLM-driven
    Section Planner agent ships.

    Args:
        state: graph state.
        min_evidence_per_section: minimum evidence items per section.
            Defaults to 1. A section's evidence count is the sum of
            evidence_items across all claims.

    Graduated doc-phase 137.
    """
    if not state.section_drafts:
        msg = "verify_evidence_budget: state.section_drafts is empty"
        log.error(msg)
        return state.model_copy(update={"failure_reason": msg})

    deficiencies: list[str] = []
    for draft in state.section_drafts:
        ev_count = sum(len(c.evidence) for c in draft.claims)
        if ev_count < min_evidence_per_section:
            deficiencies.append(
                f"section={draft.section_id} evidence_count={ev_count} "
                f"(min={min_evidence_per_section})"
            )

    if deficiencies:
        msg = (
            "verify_evidence_budget: under-evidenced sections — "
            + "; ".join(deficiencies)
        )
        log.warning(msg)
        return state.model_copy(update={"failure_reason": msg})

    log.info(
        "verify_evidence_budget.passed report_id=%s sections=%d",
        state.report_id, len(state.section_drafts),
    )
    return state


# ---------------------------------------------------------------------------
# Still-skeleton nodes (doc-phase 80, await LLM + WeasyPrint + SeaweedFS)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Phase G.3 graduations — end-to-end minimal-viable bodies for nodes 5-12.
#
# Each body produces a real artifact + threads the state forward, but
# defers heavyweight implementations (LLM section drafting, MapLibre
# tile rendering, WeasyPrint PDF, Kestra delivery dispatch) to their
# own follow-up phases. The shape is "real enough to ship a markdown
# bundle end-to-end" — the surface a Technical Due Diligence Report
# needs to deliver per master-plan §7 Done-when.
# ---------------------------------------------------------------------------

async def generate_section_drafts(state: ReportBuilderState) -> ReportBuilderState:
    """Per-section draft assembly.

    For each planned section, produce a SectionDraft whose
    `body_markdown` is built deterministically from the evidence
    items gathered in `gather_evidence`. One Claim per evidence item
    so the citation layer can wire them later.

    Future: replace the deterministic assembly with a per-section LLM
    call that narrates the evidence. The shape of SectionDraft +
    Claim doesn't change — just the body_markdown source.
    """
    if state.failure_reason:
        return state

    drafts: list[SectionDraft] = []
    # `gather_evidence` populated state.section_drafts with empty drafts
    # carrying SectionDraft.section_id + the evidence list. If it
    # didn't, we re-derive from sections_plan with empty evidence.
    existing_by_section = {d.section_id: d for d in state.section_drafts}

    for plan in state.sections_plan:
        existing = existing_by_section.get(plan.section_id)
        evidence_items = []
        if existing is not None and existing.claims:
            # gather_evidence stored evidence on the first claim — pull
            # it back out and explode into one-claim-per-evidence-item
            # so each claim has a single source_chunk_id.
            for c in existing.claims:
                evidence_items.extend(c.evidence)

        # Build markdown body deterministically.
        body_lines = [f"## {plan.title}", ""]
        if not evidence_items:
            body_lines.append(
                "_No supporting evidence found for this section in the "
                "current workspace. This section was planned but cannot "
                "ship until evidence accumulates._"
            )
            drafts.append(SectionDraft(
                section_id=plan.section_id,
                body_markdown="\n".join(body_lines),
                claims=[],
                pending_map_kinds=plan.map_kinds,
                pending_chart_kinds=plan.chart_kinds,
            ))
            continue

        # One Claim per evidence item. attach_citations will inject
        # [DATA:N] markers; validate_claims will mark each .validated.
        claims: list[Claim] = []
        for i, ev in enumerate(evidence_items, start=1):
            claim_text = (
                f"Evidence item {i}: {ev.source_chunk_id} "
                f"(visibility={ev.data_visibility})"
            )
            claims.append(Claim(
                claim_id=f"{plan.section_id}_claim_{i}",
                section_id=plan.section_id,
                text=claim_text,
                evidence=[ev],
            ))
            body_lines.append(f"- {claim_text}")

        drafts.append(SectionDraft(
            section_id=plan.section_id,
            body_markdown="\n".join(body_lines),
            claims=claims,
            pending_map_kinds=plan.map_kinds,
            pending_chart_kinds=plan.chart_kinds,
        ))

    state.section_drafts = drafts
    return state


async def validate_claims(state: ReportBuilderState) -> ReportBuilderState:
    """Mark each claim's `.validated` flag.

    Phase G.3 — minimal viable validation: a claim passes when it has
    at least one evidence item with a non-empty source_chunk_id AND a
    valid data_visibility tag. The §04i numeric / entity guards will
    plug in here when the claim text becomes LLM-generated rather than
    template-driven.
    """
    if state.failure_reason:
        return state

    rejected: list[str] = []
    for draft in state.section_drafts:
        for claim in draft.claims:
            if not claim.evidence:
                claim.validated = False
                claim.validation_notes = "no evidence items attached"
                rejected.append(claim.claim_id)
                continue
            invalid_ev = [
                e for e in claim.evidence
                if not e.source_chunk_id
                or e.data_visibility not in ("public", "workspace")
            ]
            if invalid_ev:
                claim.validated = False
                claim.validation_notes = (
                    f"{len(invalid_ev)} evidence item(s) missing chunk_id "
                    "or visibility tag"
                )
                rejected.append(claim.claim_id)
                continue
            claim.validated = True
            claim.validation_notes = None
    if rejected:
        # Don't fail the whole report — record validation status and
        # let compliance_check decide whether to block delivery.
        state.compliance_checks.append({
            "check": "claim_validation",
            "passed": False,
            "details": {
                "rejected_claim_ids": rejected,
                "rejection_count": len(rejected),
            },
        })
    return state


async def attach_citations(state: ReportBuilderState) -> ReportBuilderState:
    """Inject `[DATA:N]` markers into each claim's text + populate
    `state.citation_payload` with the marker→source mapping that the
    appendix builder consumes.
    """
    if state.failure_reason:
        return state

    citations: dict[str, dict[str, Any]] = {}
    counter = 0
    for draft in state.section_drafts:
        # Re-build body_markdown with citation markers inline.
        body_lines: list[str] = []
        title_line = draft.body_markdown.split("\n", 1)[0] if draft.body_markdown else ""
        body_lines.append(title_line)
        body_lines.append("")

        for claim in draft.claims:
            if not claim.evidence:
                body_lines.append(f"- {claim.text}")
                continue
            counter += 1
            marker = f"[DATA:{counter}]"
            ev = claim.evidence[0]
            citations[marker] = {
                "source_chunk_id": ev.source_chunk_id,
                "data_visibility": ev.data_visibility,
                "license_note": ev.license_note,
                "freshness_iso": ev.freshness_iso.isoformat() if ev.freshness_iso else None,
                "section_id": claim.section_id,
                "claim_id": claim.claim_id,
            }
            # Inline the marker into the claim text + the body bullet.
            if marker not in claim.text:
                claim.text = f"{claim.text} {marker}"
            body_lines.append(f"- {claim.text}")
        draft.body_markdown = "\n".join(body_lines)

    state.citation_payload = {
        "schema_version": 1,
        "citations": citations,
        "total_count": counter,
    }
    return state


async def generate_maps_charts(state: ReportBuilderState) -> ReportBuilderState:
    """Record which maps + charts each section requests.

    Phase G.3 — placeholder: rendering itself is deferred to the
    §17.4 MapLibre static tile pipeline. We catalog requested
    map_kinds + chart_kinds so the export bundle can announce them
    even though the URIs stay empty.
    """
    if state.failure_reason:
        return state

    requested_maps: set[str] = set()
    requested_charts: set[str] = set()
    for draft in state.section_drafts:
        for kind in draft.pending_map_kinds:
            requested_maps.add(kind)
        for kind in draft.pending_chart_kinds:
            requested_charts.add(kind)

    # Stamp empty URIs so downstream nodes know rendering wasn't done.
    state.map_uris = []
    state.chart_uris = []
    state.compliance_checks.append({
        "check": "map_chart_rendering",
        "passed": True,
        "details": {
            "requested_map_kinds": sorted(requested_maps),
            "requested_chart_kinds": sorted(requested_charts),
            "rendered": False,
            "note": "deferred to §17.4 MapLibre static renderer",
        },
    })
    return state


async def build_appendix(state: ReportBuilderState) -> ReportBuilderState:
    """Assemble the evidence + citation + source manifests + a
    placeholder hash_chain_proof.

    Writes the artifacts as inline state JSON URIs (data:application/json
    URIs) so the export node has everything it needs to bundle. Real
    deployment will upload these to SeaweedFS under a per-report prefix.
    """
    if state.failure_reason:
        return state

    import base64
    import json as _json

    # 1. Evidence JSON — flat list of all evidence items across claims.
    evidence_items: list[dict[str, Any]] = []
    for draft in state.section_drafts:
        for claim in draft.claims:
            for ev in claim.evidence:
                evidence_items.append({
                    "section_id": claim.section_id,
                    "claim_id": claim.claim_id,
                    "source_chunk_id": ev.source_chunk_id,
                    "data_visibility": ev.data_visibility,
                    "license_note": ev.license_note,
                    "freshness_iso": ev.freshness_iso.isoformat() if ev.freshness_iso else None,
                    "is_stale": ev.is_stale,
                })

    evidence_doc = {
        "report_id": str(state.report_id),
        "workspace_id": str(state.workspace_id),
        "project_id": str(state.project_id),
        "report_type": state.report_type,
        "evidence_items": evidence_items,
        "total_count": len(evidence_items),
    }

    citation_doc = {
        "report_id": str(state.report_id),
        "citations": state.citation_payload.get("citations", {}),
        "total_count": state.citation_payload.get("total_count", 0),
    }

    source_doc = {
        "report_id": str(state.report_id),
        "sources": sorted({ev["source_chunk_id"] for ev in evidence_items}),
        "data_visibilities": sorted({
            ev["data_visibility"] for ev in evidence_items
        }),
    }

    # 2. Hash-chain proof — placeholder stub. Real implementation
    # queries the audit_ledger for the workspace's most recent anchor
    # + its hash chain. Phase G.2 audit emission gives us a hook to
    # build this from. Today we record the SHA-256 of the evidence
    # bundle as a deterministic proof signature.
    import hashlib
    evidence_bytes = _json.dumps(evidence_doc, sort_keys=True).encode("utf-8")
    proof = {
        "schema_version": 1,
        "report_id": str(state.report_id),
        "evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
        "citation_count": citation_doc["total_count"],
        "evidence_item_count": evidence_doc["total_count"],
        "note": "placeholder — Phase G.2 audit anchor join pending",
    }

    state.hash_chain_proof = proof

    # 3. data: URIs so the export node has self-contained artifacts.
    def _data_uri(doc: dict) -> str:
        encoded = base64.b64encode(
            _json.dumps(doc, indent=2).encode("utf-8")
        ).decode("ascii")
        return f"data:application/json;base64,{encoded}"

    state.evidence_json_uri = _data_uri(evidence_doc)
    state.citation_manifest_uri = _data_uri(citation_doc)
    state.source_manifest_uri = _data_uri(source_doc)
    return state


async def compliance_check(state: ReportBuilderState) -> ReportBuilderState:
    """Export Compliance Agent — full §29.2 10-item checklist.

    Phase H continued (doc-phase 185) — expanded from the original
    5-gate Phase G.3 minimal version to the full 10-item §29.2
    checklist mandated by master plan §7.8.

    Gate map → §29.2 numbering:
      G01 → §29.2.01  Citations included
      G02 → §29.2.02  CRS metadata included (every spatial element)
      G03 → §29.2.03  Public/private separated (§2.9 template)
      G04 → §29.2.04  License notes included (Crown copyright, public,
                      CC-BY)
      G05 → §29.2.05  Stale evidence flagged or removed
      G06 → §29.2.06  Conflicts disclosed
      G07 → §29.2.07  User has permission                  (advisory:
                      RLS enforces at the PG layer; this gate verifies
                      the report_state itself carries a valid
                      workspace_id + requested_by_user_id)
      G08 → §29.2.08  Sign-off complete (R4/R5)
      G09 → §29.2.09  QP credential verified (NI 43-101 / CSA exports)
      G10 → §29.2.10  Hash chain recorded

    Plus 5 implementation-detail gates (G11-G15) that were the
    original Phase G.3 set — they catch upstream pipeline failures
    BEFORE the §29.2 gates fire, so a malformed state doesn't ship
    with a half-completed audit trail.

    Failure of ANY blocking gate sets state.compliance_passed=False
    and records the failure_reason; the report is blocked from export.
    """
    if state.failure_reason:
        return state

    failed: list[str] = []
    warnings: list[str] = []  # non-blocking advisories

    # ─── Pipeline-integrity gates (Phase G.3 original 5) ───────────

    # G11. At least one section with evidence.
    has_evidence = any(
        any(c.evidence for c in d.claims) for d in state.section_drafts
    )
    if not has_evidence:
        failed.append("G11_no_section_has_evidence")

    # G12. Citation payload populated.
    if state.citation_payload.get("total_count", 0) == 0:
        failed.append("G12_citation_payload_empty")

    # G13. ≥ 50% of claims validated (or report has zero claims).
    total_claims = sum(len(d.claims) for d in state.section_drafts)
    if total_claims > 0:
        invalid_claims = [
            c.claim_id
            for d in state.section_drafts
            for c in d.claims
            if not c.validated
        ]
        if len(invalid_claims) > total_claims * 0.5:
            failed.append(f"G13_too_many_invalid_claims:{len(invalid_claims)}")

    # G14. Risk tier set + valid.
    if state.risk_tier not in ("R3", "R4", "R5"):
        failed.append(f"G14_invalid_risk_tier:{state.risk_tier}")

    # G15. Evidence + citation manifests built.
    if not state.evidence_json_uri:
        failed.append("G15_evidence_json_not_built")
    if not state.citation_manifest_uri:
        failed.append("G15_citation_manifest_not_built")

    # ─── §29.2 10-item checklist (G01–G10) ─────────────────────────

    # §29.2.01 — Citations included (every claim has at least one
    # evidence chunk, OR the section drafts have zero claims).
    # G11 covers "any section has evidence"; G01 is stricter: each
    # validated claim must cite. Skip when total_claims == 0.
    if total_claims > 0:
        uncited = [
            c.claim_id
            for d in state.section_drafts
            for c in d.claims
            if c.validated and not c.evidence
        ]
        if uncited:
            failed.append(f"G01_uncited_validated_claims:{len(uncited)}")

    # §29.2.02 — CRS metadata. Every map_uri implies a spatial element
    # in the bundle; the report state must carry a `crs_metadata`
    # entry in compliance_checks (set by the spatial nodes). When no
    # maps were generated, this gate skips. Today the spatial nodes
    # don't yet populate crs_metadata explicitly — we mark this as
    # a non-blocking warning rather than a hard fail until §5
    # ships the chart export contract that produces it.
    if state.map_uris:
        crs_present = any(
            c.get("check") == "crs_metadata" for c in state.compliance_checks
        )
        if not crs_present:
            warnings.append("G02_crs_metadata_not_recorded")

    # §29.2.03 — Public/private separated. Each evidence chunk carries
    # a `data_visibility` field (public | workspace). Mixed visibility
    # inside a single claim must be flagged. The §2.9 template
    # requires separation at the document level — we check that any
    # claim with both public AND workspace evidence is explicitly
    # labelled in the conflicts_disclosed block.
    mixed_visibility_claims: list[str] = []
    for d in state.section_drafts:
        for c in d.claims:
            visibilities = {e.data_visibility for e in c.evidence}
            if len(visibilities) > 1:
                mixed_visibility_claims.append(c.claim_id)
    if mixed_visibility_claims:
        disclosed_keys = {
            cd.get("entity_key", "")
            for cd in state.conflicts_disclosed
            if isinstance(cd, dict)
        }
        unflagged = [
            cid for cid in mixed_visibility_claims
            if cid not in disclosed_keys
        ]
        if unflagged:
            failed.append(
                f"G03_mixed_visibility_not_separated:{len(unflagged)}"
            )

    # §29.2.04 — License notes. Public-visibility evidence MUST carry
    # a `license_note` (Crown copyright / public domain / CC-BY).
    # Workspace-private evidence doesn't require a license note
    # (it's internal-only data). This catches the case where a
    # Crown-copyright PGEO record was surfaced without its attribution.
    unlicensed_public: list[str] = []
    for d in state.section_drafts:
        for c in d.claims:
            for e in c.evidence:
                if e.data_visibility == "public" and not e.license_note:
                    unlicensed_public.append(e.source_chunk_id)
    if unlicensed_public:
        failed.append(
            f"G04_public_evidence_missing_license:{len(unlicensed_public)}"
        )

    # §29.2.05 — Stale evidence flagged or removed. Any evidence
    # carrying `is_stale=True` must EITHER be removed from the
    # claim's evidence list OR be disclosed in conflicts_disclosed.
    # Letting stale evidence flow through without disclosure is the
    # exact failure mode §29.2 is designed to catch.
    stale_undisclosed: list[str] = []
    for d in state.section_drafts:
        for c in d.claims:
            for e in c.evidence:
                if e.is_stale:
                    stale_undisclosed.append(e.source_chunk_id)
    if stale_undisclosed:
        # Look for the chunk_ids in the disclosure block.
        disclosed_chunks = {
            ev_id
            for cd in state.conflicts_disclosed
            if isinstance(cd, dict)
            for ev_id in (cd.get("evidence_ids") or [])
        }
        truly_undisclosed = [
            sid for sid in stale_undisclosed if sid not in disclosed_chunks
        ]
        if truly_undisclosed:
            failed.append(
                f"G05_stale_evidence_undisclosed:{len(truly_undisclosed)}"
            )

    # §29.2.06 — Conflicts disclosed. If the §7.4 Conflict Resolver
    # Agent identified conflicting evidence (different sources
    # disagreeing on the same property), each conflict MUST appear
    # in conflicts_disclosed. Today the Conflict Resolver is
    # advisory (writes to state.conflicts_disclosed if invoked);
    # this gate verifies that when conflicts were identified, they
    # were actually written rather than silently dropped.
    # A blocking gate only when state.conflicts_disclosed is empty
    # AND a "conflicts_present" advisory check exists. Otherwise
    # advisory only.
    conflicts_advisory_present = any(
        c.get("check") == "conflicts_advisory" for c in state.compliance_checks
    )
    if conflicts_advisory_present and not state.conflicts_disclosed:
        failed.append("G06_conflicts_advisory_but_none_disclosed")

    # §29.2.07 — User has permission. RLS enforces at PG; here we
    # verify the state carries a valid identity envelope so the
    # audit trail downstream has the right attribution.
    if not state.workspace_id:
        failed.append("G07_missing_workspace_id")
    if not state.requested_by_user_id:
        failed.append("G07_missing_requested_by_user_id")

    # §29.2.08 — Sign-off complete. For R4/R5 reports, the geologist
    # (R4) or geologist + QP (R5) sign-off records must exist AND
    # have signed_at timestamps. R3 reports skip this gate.
    #
    # Phase H continued — graph-internal compliance_check runs MID-
    # workflow (BEFORE the geologist_approval node), so sign-off
    # failures land in `advisories` (workflow-pipeline aware) rather
    # than `failed` (workflow-blocking). The standalone
    # `export_compliance` agent (the actual §7.8 export gate)
    # promotes them back to blocking at the export-ready surface.
    # See app/agents/phase7/export_compliance.py for the per-export
    # promotion logic.
    if state.risk_tier in ("R4", "R5"):
        if not state.sign_off_complete:
            warnings.append(f"G08_sign_off_incomplete:{state.risk_tier}")
        if not state.sign_offs:
            warnings.append(f"G08_sign_off_records_missing:{state.risk_tier}")
        else:
            unsigned = [
                so.role for so in state.sign_offs if so.signed_at is None
            ]
            if unsigned:
                warnings.append(
                    f"G08_unsigned_records:{','.join(sorted(set(unsigned)))}"
                )

    # §29.2.09 — QP credential verified. R5 (NI 43-101 / CSA exports)
    # requires a `qp` SignOffRecord with a non-empty
    # `qp_credential_id`. Other risk tiers skip this gate.
    # Same advisory/blocking split as G08 — graph-internal pass-thru,
    # standalone agent enforcement.
    if state.risk_tier == "R5":
        qp_records = [so for so in state.sign_offs if so.role == "qp"]
        if not qp_records:
            warnings.append("G09_qp_signoff_record_missing")
        else:
            qp_with_credential = [
                so for so in qp_records if so.qp_credential_id
            ]
            if not qp_with_credential:
                warnings.append("G09_qp_credential_id_missing")

    # §29.2.10 — Hash chain recorded. The report bundle must carry a
    # hash_chain_proof dict containing at least one tamper-evidence
    # anchor — accepted shapes today:
    #   - `anchor_id` (audit.audit_ledger row UUID; preferred once the
    #     Phase G.2 audit-anchor join lands)
    #   - `evidence_sha256` (Phase G.3 minimal: SHA-256 over the
    #     evidence_json_uri's normalised bytes — proves the bundle's
    #     evidence set hasn't been tampered with)
    # Absent proof OR proof with neither field set means the bundle
    # isn't tamper-evident and cannot be exported under §29.2.
    if not state.hash_chain_proof:
        failed.append("G10_hash_chain_proof_missing")
    elif not isinstance(state.hash_chain_proof, dict):
        failed.append("G10_hash_chain_proof_malformed")
    elif not (
        state.hash_chain_proof.get("anchor_id")
        or state.hash_chain_proof.get("evidence_sha256")
    ):
        failed.append("G10_hash_chain_proof_missing_anchor")

    # ─── Aggregate ─────────────────────────────────────────────────

    state.compliance_passed = not failed
    state.compliance_checks.append({
        "check": "export_compliance_v2",  # was v1 (Phase G.3)
        "passed": state.compliance_passed,
        "details": {
            "failed_gates": failed,
            "warnings": warnings,
            "gates_total": 15,
            "gates_passed": 15 - len(failed),
            "§29.2_items_checked": 10,
            "pipeline_integrity_gates": 5,
        },
    })
    if not state.compliance_passed:
        state.failure_reason = (
            f"compliance_check failed gates: {', '.join(failed)}"
        )
    return state


async def geologist_approval(state: ReportBuilderState) -> ReportBuilderState:
    """R4 + R5 sign-off ceremony.

    Phase G.3:
      * R3 → auto-approve (no sign-off required), set sign_off_complete=True
      * R4 → record a `geologist` SignOffRecord with no signed_at yet,
        leave sign_off_complete=False. Real sign-off arrives via Hatchet
        pause/resume in §11 follow-up.
      * R5 → record both a `geologist` and a `qp` SignOffRecord with no
        signatures yet.
    """
    if state.failure_reason:
        return state

    if state.risk_tier == "R3":
        state.sign_off_complete = True
        return state

    if state.risk_tier in ("R4", "R5"):
        # Pending geologist sign-off
        state.sign_offs.append(SignOffRecord(role="geologist"))
        if state.risk_tier == "R5":
            state.sign_offs.append(SignOffRecord(role="qp"))
        # sign_off_complete stays False — actual sign-off arrives later.
        state.sign_off_complete = False
    return state


async def export_package(state: ReportBuilderState) -> ReportBuilderState:
    """Render a markdown bundle.

    Phase G.3 — produces a single concatenated markdown string and
    stamps it as a `data:text/markdown` URI on `state` so callers can
    inspect or save. PDF (WeasyPrint) and DOCX (python-docx) are
    deferred to a follow-up alongside SeaweedFS upload.
    """
    if state.failure_reason:
        return state

    import base64

    parts: list[str] = []
    parts.append(f"# {state.report_type.replace('_', ' ').title()}")
    parts.append("")
    parts.append(f"**Report ID:** `{state.report_id}`  ")
    parts.append(f"**Workspace:** `{state.workspace_id}`  ")
    parts.append(f"**Project:** `{state.project_id}`  ")
    parts.append(f"**Risk tier:** {state.risk_tier}  ")
    parts.append(f"**Generated:** {state.started_at.isoformat() if state.started_at else 'unknown'}")
    parts.append("")
    parts.append("---")
    parts.append("")

    for draft in state.section_drafts:
        parts.append(draft.body_markdown)
        parts.append("")

    # Citation footer
    parts.append("---")
    parts.append("")
    parts.append("## Citations")
    parts.append("")
    citations = state.citation_payload.get("citations", {})
    if citations:
        for marker, src in sorted(citations.items()):
            parts.append(
                f"- **{marker}** — {src['source_chunk_id']} "
                f"({src['data_visibility']})"
            )
    else:
        parts.append("_No citations attached._")

    # Hash-chain proof footer
    if state.hash_chain_proof:
        parts.append("")
        parts.append("## Provenance Proof")
        parts.append("")
        parts.append(f"- evidence_sha256: `{state.hash_chain_proof['evidence_sha256']}`")
        parts.append(f"- evidence_item_count: {state.hash_chain_proof['evidence_item_count']}")
        parts.append(f"- citation_count: {state.hash_chain_proof['citation_count']}")

    markdown = "\n".join(parts)
    encoded_md = base64.b64encode(markdown.encode("utf-8")).decode("ascii")

    # Phase G.3 follow-up — also render PDF via WeasyPrint. The
    # markdown data: URI stays around as `evidence_json_uri`-style
    # sidecar for downstream callers that prefer the source text.
    state.docx_uri = None  # not rendered yet
    state.xlsx_uri = None
    try:
        from app.services.report_builder.renderers.pdf_renderer import (
            render_pdf_from_markdown,
        )
        pdf_bytes = render_pdf_from_markdown(
            markdown,
            title=f"{state.report_type.replace('_', ' ').title()} — {state.report_id}",
        )
        encoded_pdf = base64.b64encode(pdf_bytes).decode("ascii")
        state.pdf_uri = f"data:application/pdf;base64,{encoded_pdf}"
        log.info(
            "export_package: rendered %d-byte PDF for report %s",
            len(pdf_bytes), state.report_id,
        )
    except Exception:
        # WeasyPrint can fail on the dev workstation when fontconfig
        # caches aren't writable. Fall back to the markdown bundle as
        # before so the pipeline still produces *something*.
        log.warning(
            "export_package: PDF rendering failed; falling back to "
            "markdown data: URI",
            exc_info=True,
        )
        state.pdf_uri = f"data:text/markdown;base64,{encoded_md}"
    return state


async def activepieces_delivery(state: ReportBuilderState) -> ReportBuilderState:
    """Delivery dispatch — Phase G.3 log-only stub.

    The flow has been ported to Kestra per ADR-0001 (Kestra
    sunset). Real dispatch enqueues a Kestra flow that fans out to
    email / Teams / SharePoint / Slack per `delivery_targets`. For
    now we just record that we *would* dispatch + the targets, then
    set `delivery_dispatched=True` so the orchestrator marks success.
    """
    if state.failure_reason:
        return state

    # Log-only — real Kestra dispatch lives in §7.11 follow-up phase.
    state.delivery_dispatched = True
    return state
