"""§7.2 what_changed report integration with §9.13 what_changed_detector.

Doc-phase 156 — wires the doc-phase 147 `what_changed_detector` Hatchet
task body into the §15.1 Report Builder graph. When the report_type
is 'what_changed', `gather_evidence` calls into this integration
instead of using the synthetic stub.

The integration:
  1. Validates the state carries report_window_start/end
  2. Invokes `what_changed_detector.execute()` via .aio_mock_run
     (bypassing the Hatchet runtime — the body is plain async)
  3. Maps the detector's structured output into per-section claims
     + evidence items
  4. Returns a list[SectionDraft] ready to drop into state.section_drafts

Section mapping:
  - period       → window metadata + total audit count
  - data_changes → silver delta counts (decisions, hypotheses, tickets)
  - claim_changes → claim_ledger delta (still synthetic; awaits §9.5 schema)
  - target_changes → target zone delta (still synthetic; awaits §18 wiring)
"""
from __future__ import annotations

import logging
from uuid import uuid4

from app.hatchet_workflows.what_changed_detector import (
    WhatChangedInput,
)
from app.hatchet_workflows.what_changed_detector import (
    execute as what_changed_execute,
)
from app.services.report_builder.state import (
    Claim,
    EvidenceItem,
    ReportBuilderState,
    SectionDraft,
)

log = logging.getLogger("georag.report_builder.whatchanged_integration")


async def gather_evidence_what_changed(
    state: ReportBuilderState,
) -> list[SectionDraft] | None:
    """Build section_drafts for a what_changed report from real
    workspace deltas.

    Returns:
        list[SectionDraft] when report_type='what_changed' AND
        report_window_start/end are set. None otherwise (caller falls
        back to the synthetic stub).
    """
    if state.report_type != "what_changed":
        return None
    if state.report_window_start is None or state.report_window_end is None:
        log.warning(
            "what_changed report %s has no window — falling back to stub",
            state.report_id,
        )
        return None

    # Invoke the §9.13 detector to get real deltas.
    inp = WhatChangedInput(
        workspace_id=state.workspace_id,
        project_id=state.project_id,
        window_start=state.report_window_start,
        window_end=state.report_window_end,
        detect_request_id=uuid4(),
    )
    detector_result = await what_changed_execute.aio_mock_run(inp)
    log.info(
        "gather_evidence_what_changed.detector_completed report_id=%s "
        "decisions=%d hypotheses=%d support=%d total_audits=%d",
        state.report_id, detector_result.new_decision_count,
        detector_result.new_hypothesis_count,
        detector_result.new_support_ticket_count,
        detector_result.total_audit_anchors_in_window,
    )

    drafts: list[SectionDraft] = []

    # period section — window metadata.
    drafts.append(
        SectionDraft(
            section_id="period",
            body_markdown=(
                f"## Reporting Period\n\n"
                f"- **Start:** {state.report_window_start.isoformat()}\n"
                f"- **End:** {state.report_window_end.isoformat()}\n"
                f"- **Total audit anchors:** "
                f"{detector_result.total_audit_anchors_in_window}\n"
            ),
            claims=[
                Claim(
                    claim_id="period.claim_window",
                    section_id="period",
                    text=(
                        f"Reporting window "
                        f"{state.report_window_start.date().isoformat()} → "
                        f"{state.report_window_end.date().isoformat()} "
                        f"saw {detector_result.total_audit_anchors_in_window} "
                        f"audit anchors emitted in this workspace."
                    ),
                    evidence=[
                        EvidenceItem(
                            source_chunk_id=(
                                f"what_changed.window."
                                f"{state.report_window_start.timestamp():.0f}"
                            ),
                            data_visibility="workspace",
                        ),
                    ],
                    validated=True,  # window metadata is intrinsic
                )
            ],
        )
    )

    # data_changes section — silver delta summary.
    drafts.append(
        SectionDraft(
            section_id="data_changes",
            body_markdown=(
                f"## Data Changes\n\n"
                f"- **New ingestions:** "
                f"{detector_result.new_ingestion_count}\n"
                f"- **New public records:** "
                f"{detector_result.new_public_record_count}\n"
                f"- **Updated public records:** "
                f"{detector_result.updated_public_record_count}\n"
                f"- **Decisions recorded:** "
                f"{detector_result.new_decision_count}\n"
                f"- **Hypotheses generated:** "
                f"{detector_result.new_hypothesis_count}\n"
                f"- **Support tickets opened:** "
                f"{detector_result.new_support_ticket_count}\n"
            ),
            claims=[
                Claim(
                    claim_id="data_changes.claim_ingestions",
                    section_id="data_changes",
                    text=(
                        f"{detector_result.new_ingestion_count} new ingestion "
                        f"events recorded in the workspace audit ledger "
                        f"during the reporting window."
                    ),
                    evidence=[
                        EvidenceItem(
                            source_chunk_id="what_changed.ingestion_anchors",
                            data_visibility="workspace",
                        ),
                    ],
                    validated=True,
                ),
                Claim(
                    claim_id="data_changes.claim_decisions",
                    section_id="data_changes",
                    text=(
                        f"{detector_result.new_decision_count} §21 decision "
                        f"records persisted in the window."
                    ),
                    evidence=[
                        EvidenceItem(
                            source_chunk_id="what_changed.decision_records",
                            data_visibility="workspace",
                        ),
                    ],
                    validated=True,
                ),
                Claim(
                    claim_id="data_changes.claim_hypotheses",
                    section_id="data_changes",
                    text=(
                        f"{detector_result.new_hypothesis_count} ai_suggested "
                        f"hypotheses generated in the window."
                    ),
                    evidence=[
                        EvidenceItem(
                            source_chunk_id="what_changed.hypothesis_anchors",
                            data_visibility="workspace",
                        ),
                    ],
                    validated=True,
                ),
            ],
        )
    )

    # claim_changes section — silver.claim_ledger doesn't exist yet (§9.5 pending).
    drafts.append(
        SectionDraft(
            section_id="claim_changes",
            body_markdown=(
                "## Claim Ledger Changes\n\n"
                "_No claim-ledger delta available — the "
                "`silver.claim_ledger` schema lands with §9.5._\n"
            ),
            claims=[
                Claim(
                    claim_id="claim_changes.claim_pending",
                    section_id="claim_changes",
                    text=(
                        "Claim-ledger delta detection pending §9.5 schema."
                    ),
                    evidence=[
                        EvidenceItem(
                            source_chunk_id="what_changed.claim_ledger_pending",
                            data_visibility="workspace",
                        ),
                    ],
                    validated=False,  # not real evidence — annotated as pending
                )
            ],
        )
    )

    # target_changes section — target zone deltas (synthetic; awaits §18 graduations).
    drafts.append(
        SectionDraft(
            section_id="target_changes",
            body_markdown=(
                "## Target Recommendation Changes\n\n"
                "_No target-zone score-shift signal available — the "
                "§18 delta detection wires in when score_targets emits "
                "score-change audits._\n"
            ),
            claims=[
                Claim(
                    claim_id="target_changes.claim_pending",
                    section_id="target_changes",
                    text=(
                        "Target-zone score-shift detection pending §18 wiring."
                    ),
                    evidence=[
                        EvidenceItem(
                            source_chunk_id="what_changed.target_delta_pending",
                            data_visibility="workspace",
                        ),
                    ],
                    validated=False,
                )
            ],
        )
    )

    return drafts


__all__ = ["gather_evidence_what_changed"]
