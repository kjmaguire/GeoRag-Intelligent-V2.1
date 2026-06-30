"""Export Compliance Agent (§7.8 / §15.4 / §29.2).

THE universal export gate. Runs as a node in every Report Builder
Graph invocation (see ``services/report_builder/nodes.py::compliance_check``)
AND as a standalone agent invokable from the cockpit / external pipelines
(ArcGIS publish, customer webhook payloads, data-room bundles).

The §29.2 10-item checklist:
  1. Citations included
  2. CRS metadata included (every spatial element)
  3. Public/private separated (§2.9 template)
  4. License notes included (Crown copyright, public domain, CC-BY)
  5. Stale evidence flagged or removed
  6. Conflicts disclosed
  7. User has permission
  8. Sign-off complete (R4/R5)
  9. QP credential verified (NI 43-101 / CSA exports)
 10. Hash chain recorded

Phase H continued (doc-phase 185) — graduated from doc-phase 78
skeleton. Behavior delegates to the same gate logic the Report
Builder Graph already runs at the ``compliance_check`` node, so
the two surfaces (graph-internal + standalone) never drift.

Output contract:
    {
        "checks": [
            {
                "name": str,           # one of the 10 §29.2 items
                "passed": bool,
                "evidence": str,       # what was checked + result
                "blocking": bool,      # if true and not passed, export blocked
            },
            ...
        ],
        "passed": bool,                # AND of all blocking checks
        "blocking_failures": [str],    # names of failed blocking checks
        "non_blocking_warnings": [str],
    }
"""
from __future__ import annotations

from typing import Any, Literal
from uuid import UUID, uuid4

from app.agents import AgentContext, georag_agent

ExportKind = Literal[
    "report_pdf",
    "report_docx",
    "report_xlsx",
    "data_room_zip",
    "arcgis_publish",
    "webhook_payload",
]


# Mapping from internal gate name (G01-G15) to a human-readable §29.2 label.
_GATE_LABELS: dict[str, tuple[str, bool]] = {
    # name: (label, blocking?)
    "G01_uncited_validated_claims":          ("§29.2.01 Citations included",            True),
    "G02_crs_metadata_not_recorded":         ("§29.2.02 CRS metadata included",         False),  # advisory
    "G03_mixed_visibility_not_separated":    ("§29.2.03 Public/private separated",      True),
    "G04_public_evidence_missing_license":   ("§29.2.04 License notes included",        True),
    "G05_stale_evidence_undisclosed":        ("§29.2.05 Stale evidence flagged",        True),
    "G06_conflicts_advisory_but_none_disclosed": ("§29.2.06 Conflicts disclosed",       True),
    "G07_missing_workspace_id":              ("§29.2.07 User has permission",           True),
    "G07_missing_requested_by_user_id":      ("§29.2.07 User has permission",           True),
    "G08_sign_off_incomplete":               ("§29.2.08 Sign-off complete",             True),
    "G08_sign_off_records_missing":          ("§29.2.08 Sign-off complete",             True),
    "G08_unsigned_records":                  ("§29.2.08 Sign-off complete",             True),
    "G09_qp_signoff_record_missing":         ("§29.2.09 QP credential verified",        True),
    "G09_qp_credential_id_missing":          ("§29.2.09 QP credential verified",        True),
    "G10_hash_chain_proof_missing":          ("§29.2.10 Hash chain recorded",           True),
    "G10_hash_chain_proof_malformed":        ("§29.2.10 Hash chain recorded",           True),
    "G10_hash_chain_proof_missing_anchor_id":("§29.2.10 Hash chain recorded",           True),
    # Pipeline-integrity gates (advisory; non-blocking for the standalone agent).
    "G11_no_section_has_evidence":           ("Pipeline G11 — section evidence",        False),
    "G12_citation_payload_empty":            ("Pipeline G12 — citation payload",        False),
    "G13_too_many_invalid_claims":           ("Pipeline G13 — claim validation",        False),
    "G14_invalid_risk_tier":                 ("Pipeline G14 — risk tier",               False),
    "G15_evidence_json_not_built":           ("Pipeline G15 — evidence JSON",           False),
    "G15_citation_manifest_not_built":       ("Pipeline G15 — citation manifest",       False),
}


def _build_state_from_payload(
    *,
    workspace_id: UUID | str,
    export_payload: dict[str, Any],
    report_id: UUID | str | None,
):
    """Construct a ReportBuilderState from a standalone agent payload.

    The standalone agent accepts a flat dict of compliance-relevant
    state slices rather than a fully populated graph state. This
    helper assembles a minimal state object the shared
    ``compliance_check`` node can read.
    """
    from app.services.report_builder.state import (  # noqa: PLC0415
        Claim,
        EvidenceItem,
        ReportBuilderState,
        SectionDraft,
        SignOffRecord,
    )

    sections_in = export_payload.get("section_drafts") or []
    section_drafts: list[SectionDraft] = []
    for s in sections_in:
        claims_in = s.get("claims") or []
        claims: list[Claim] = []
        for c in claims_in:
            evidence_in = c.get("evidence") or []
            evidence: list[EvidenceItem] = []
            for e in evidence_in:
                if isinstance(e, dict):
                    try:
                        evidence.append(EvidenceItem(
                            source_chunk_id=str(e.get("source_chunk_id", "")),
                            data_visibility=e.get("data_visibility") or "workspace",
                            license_note=e.get("license_note"),
                            is_stale=bool(e.get("is_stale", False)),
                            freshness_iso=e.get("freshness_iso"),
                        ))
                    except Exception:
                        continue
            try:
                claims.append(Claim(
                    claim_id=str(c.get("claim_id", "")),
                    section_id=str(s.get("section_id", "")),
                    text=str(c.get("text", "")),
                    evidence=evidence,
                    validated=bool(c.get("validated", False)),
                ))
            except Exception:
                continue
        try:
            section_drafts.append(SectionDraft(
                section_id=str(s.get("section_id", "")),
                body_markdown=str(s.get("body_markdown", s.get("draft_text", ""))),
                claims=claims,
            ))
        except Exception:
            continue

    sign_offs_in = export_payload.get("sign_offs") or []
    sign_offs: list[SignOffRecord] = []
    for so in sign_offs_in:
        if isinstance(so, dict):
            try:
                sign_offs.append(SignOffRecord(
                    role=so.get("role", "geologist"),
                    user_id=so.get("user_id"),
                    qp_credential_id=so.get("qp_credential_id"),
                    signed_at=so.get("signed_at"),
                    audit_ledger_id=so.get("audit_ledger_id"),
                ))
            except Exception:
                continue

    return ReportBuilderState(
        report_id=(
            UUID(str(report_id)) if report_id else uuid4()
        ),
        workspace_id=UUID(str(workspace_id)),
        project_id=UUID(str(export_payload.get("project_id"))) if export_payload.get("project_id") else UUID(str(workspace_id)),
        report_type=export_payload.get("report_type", "weekly_project_digest"),
        risk_tier=export_payload.get("risk_tier", "R3"),
        requested_by_user_id=int(export_payload.get("requested_by_user_id", 0) or 0),
        section_drafts=section_drafts,
        citation_payload=export_payload.get("citation_payload") or {},
        conflicts_disclosed=export_payload.get("conflicts_disclosed") or [],
        sign_offs=sign_offs,
        sign_off_complete=bool(export_payload.get("sign_off_complete", False)),
        evidence_json_uri=export_payload.get("evidence_json_uri"),
        citation_manifest_uri=export_payload.get("citation_manifest_uri"),
        source_manifest_uri=export_payload.get("source_manifest_uri"),
        map_uris=list(export_payload.get("map_uris") or []),
        hash_chain_proof=export_payload.get("hash_chain_proof"),
    )


def _format_check_result(state) -> dict[str, Any]:
    """Render the agent's structured output from compliance_check state.

    The standalone agent is the actual §7.8 EXPORT gate — invoked when
    a bundle is about to be shipped (PDF download, ArcGIS publish,
    webhook payload, data-room ZIP). At that point sign-off gates
    (G08/G09) ARE blocking. The graph-internal compliance_check runs
    earlier (mid-workflow) and treats them as warnings so the workflow
    body can pause at the geologist_approval node without failing.
    """
    # The last entry in compliance_checks is the freshly-appended v2 result.
    if not state.compliance_checks:
        return {
            "checks": [],
            "passed": False,
            "blocking_failures": ["compliance_check did not run"],
            "non_blocking_warnings": [],
        }

    result = state.compliance_checks[-1]
    details = result.get("details") or {}
    failed_gates = list(details.get("failed_gates") or [])
    raw_warnings = list(details.get("warnings") or [])

    # Phase H continued — promote sign-off / QP-credential warnings to
    # blocking failures at the export surface. The graph-internal
    # compliance_check (which produced this state) only added them to
    # warnings; the standalone agent enforces them.
    promoted: list[str] = []
    residual_warnings: list[str] = []
    for w in raw_warnings:
        if w.startswith("G08_") or w.startswith("G09_"):
            promoted.append(w)
        else:
            residual_warnings.append(w)
    failed_gates.extend(promoted)

    checks: list[dict[str, Any]] = []
    blocking_failures: list[str] = []
    non_blocking_warnings: list[str] = residual_warnings

    # Build a per-gate check entry. A gate that DIDN'T fail simply
    # doesn't have a failure record; we still synthesize a "passed"
    # check for the 10 §29.2 items so the cockpit UI sees the full
    # checklist outcome.
    failed_prefixes = {f.split(":", 1)[0] for f in failed_gates}
    seen_labels: set[str] = set()
    for gate_name, fail_token in [(g.split(":", 1)[0], g) for g in failed_gates]:
        label, blocking = _GATE_LABELS.get(gate_name, (gate_name, True))
        if label in seen_labels:
            continue
        seen_labels.add(label)
        checks.append({
            "name":     label,
            "passed":   False,
            "evidence": fail_token,
            "blocking": blocking,
        })
        if blocking:
            blocking_failures.append(label)

    # Emit a "passed" entry for each §29.2 item that didn't fail.
    for gate_name, (label, blocking) in _GATE_LABELS.items():
        if label.startswith("§29.2") and label not in seen_labels:
            if gate_name.split("_")[0] not in failed_prefixes:
                seen_labels.add(label)
                checks.append({
                    "name":     label,
                    "passed":   True,
                    "evidence": "checklist gate passed",
                    "blocking": blocking,
                })

    return {
        "checks":                checks,
        "passed":                len(blocking_failures) == 0,
        "blocking_failures":     blocking_failures,
        "non_blocking_warnings": non_blocking_warnings,
    }


@georag_agent(
    name="Export Compliance Agent",
    risk_tier="R3",  # Blocks export; one tier above R2 boundary agent
    version="1.0.0",  # graduated doc-phase 185
)
async def export_compliance(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    export_kind: ExportKind,
    report_id: UUID | str | None = None,
    export_payload: dict[str, Any],
) -> dict[str, Any]:
    """Run the §29.2 10-item export compliance checklist.

    Args:
        workspace_id: workspace context — permission + RLS scope.
        export_kind: shape of the export being validated.
        report_id: when export_kind is a report variant, the report
            id whose audit ledger entries we'll verify against.
        export_payload: structured export bundle to inspect
            (section_drafts, citation_payload, conflicts_disclosed,
            sign_offs, hash_chain_proof, etc.).

    Returns:
        Compliance result (see module docstring for schema).

    Behavior — graduated doc-phase 185. Delegates to the same
    ``compliance_check`` node the Report Builder Graph runs, so
    both surfaces share the gate logic and outcomes never drift.
    """
    from app.services.report_builder.nodes import (  # noqa: PLC0415
        compliance_check,
    )

    state = _build_state_from_payload(
        workspace_id=workspace_id,
        export_payload=export_payload,
        report_id=report_id,
    )
    state = await compliance_check(state)
    return _format_check_result(state)
