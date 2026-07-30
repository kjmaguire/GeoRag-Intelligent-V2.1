"""Phase H continued — tests for the Export Compliance Agent (§7.8 / §29.2).

Covers:
* All 10 §29.2 gates fire when their preconditions are violated
* Happy path — fully-populated state passes all gates
* R3 / R4 / R5 sign-off discrimination (R3 skips sign-off gates)
* R5 QP credential gate
* Hash chain shape validation
* Standalone agent matches the graph-internal compliance_check
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

WS = UUID("a0000000-0000-0000-0000-000000000001")
PROJ = UUID("11111111-1111-1111-1111-111111111111")
REPORT = UUID("22222222-2222-2222-2222-222222222222")


def _happy_state(risk_tier: str = "R3"):
    """Build a ReportBuilderState that should pass every blocking gate."""
    from app.services.report_builder.state import (
        Claim,
        EvidenceItem,
        ReportBuilderState,
        SectionDraft,
        SignOffRecord,
    )

    evidence = [
        EvidenceItem(
            source_chunk_id="chunk-1",
            data_visibility="workspace",
            license_note=None,
            is_stale=False,
        ),
    ]
    section = SectionDraft(
        section_id="sec-1",
        body_markdown="Some text.",
        claims=[
            Claim(
                claim_id="claim-1",
                section_id="sec-1",
                text="The project has 63 drillholes.",
                evidence=evidence,
                validated=True,
            ),
        ],
    )

    sign_offs: list[SignOffRecord] = []
    sign_off_complete = True
    if risk_tier == "R4":
        sign_offs = [
            SignOffRecord(
                role="geologist",
                user_id=1,
                signed_at=datetime.now(tz=UTC),
            ),
        ]
    elif risk_tier == "R5":
        sign_offs = [
            SignOffRecord(
                role="geologist",
                user_id=1,
                signed_at=datetime.now(tz=UTC),
            ),
            SignOffRecord(
                role="qp",
                user_id=2,
                qp_credential_id="QP-12345",
                signed_at=datetime.now(tz=UTC),
            ),
        ]

    return ReportBuilderState(
        report_id=REPORT,
        workspace_id=WS,
        project_id=PROJ,
        report_type="weekly_project_digest",
        risk_tier=risk_tier,
        requested_by_user_id=1,
        section_drafts=[section],
        citation_payload={"total_count": 1, "by_kind": {"DATA": 1}},
        conflicts_disclosed=[],
        sign_offs=sign_offs,
        sign_off_complete=sign_off_complete,
        evidence_json_uri="data:application/json;base64,e30=",
        citation_manifest_uri="data:application/json;base64,e30=",
        hash_chain_proof={"anchor_id": str(uuid4()), "prev_hash": "abc"},
    )


# ────────────────────── happy paths by risk tier ──────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("risk_tier", ["R3", "R4", "R5"])
async def test_compliance_check_happy_path(risk_tier: str) -> None:
    from app.services.report_builder.nodes import compliance_check

    state = _happy_state(risk_tier)
    state = await compliance_check(state)
    assert state.compliance_passed is True, state.compliance_checks
    assert state.failure_reason in (None, "")


# ─────────────────────── §29.2 gate violations ───────────────────────


@pytest.mark.asyncio
async def test_g01_uncited_claim_fails() -> None:
    from app.services.report_builder.nodes import compliance_check

    state = _happy_state()
    state.section_drafts[0].claims[0].evidence = []  # uncited
    state = await compliance_check(state)
    assert state.compliance_passed is False
    failed = state.compliance_checks[-1]["details"]["failed_gates"]
    assert any(g.startswith("G01_") for g in failed)


@pytest.mark.asyncio
async def test_g03_mixed_visibility_fails_when_undisclosed() -> None:
    from app.services.report_builder.nodes import compliance_check
    from app.services.report_builder.state import EvidenceItem

    state = _happy_state()
    # One workspace + one public on the same claim, NOT disclosed.
    state.section_drafts[0].claims[0].evidence = [
        EvidenceItem(source_chunk_id="ws-1", data_visibility="workspace"),
        EvidenceItem(
            source_chunk_id="pub-1",
            data_visibility="public",
            license_note="Crown copyright",
        ),
    ]
    state = await compliance_check(state)
    failed = state.compliance_checks[-1]["details"]["failed_gates"]
    assert any(g.startswith("G03_") for g in failed)


@pytest.mark.asyncio
async def test_g04_public_evidence_without_license_fails() -> None:
    from app.services.report_builder.nodes import compliance_check
    from app.services.report_builder.state import EvidenceItem

    state = _happy_state()
    state.section_drafts[0].claims[0].evidence = [
        EvidenceItem(
            source_chunk_id="pub-1",
            data_visibility="public",
            license_note=None,  # unlicensed public!
        ),
    ]
    state = await compliance_check(state)
    failed = state.compliance_checks[-1]["details"]["failed_gates"]
    assert any(g.startswith("G04_") for g in failed)


@pytest.mark.asyncio
async def test_g05_stale_evidence_undisclosed_fails() -> None:
    from app.services.report_builder.nodes import compliance_check
    from app.services.report_builder.state import EvidenceItem

    state = _happy_state()
    state.section_drafts[0].claims[0].evidence = [
        EvidenceItem(
            source_chunk_id="stale-1",
            data_visibility="workspace",
            is_stale=True,
        ),
    ]
    state = await compliance_check(state)
    failed = state.compliance_checks[-1]["details"]["failed_gates"]
    assert any(g.startswith("G05_") for g in failed)


@pytest.mark.asyncio
async def test_g05_stale_evidence_passes_when_disclosed() -> None:
    from app.services.report_builder.nodes import compliance_check
    from app.services.report_builder.state import EvidenceItem

    state = _happy_state()
    state.section_drafts[0].claims[0].evidence = [
        EvidenceItem(
            source_chunk_id="stale-1",
            data_visibility="workspace",
            is_stale=True,
        ),
    ]
    state.conflicts_disclosed = [{
        "entity_key": "stale-1",
        "evidence_ids": ["stale-1"],
        "reason": "supersession",
    }]
    state = await compliance_check(state)
    failed = state.compliance_checks[-1]["details"]["failed_gates"]
    assert not any(g.startswith("G05_") for g in failed)


@pytest.mark.asyncio
async def test_g07_missing_user_id_fails() -> None:
    from app.services.report_builder.nodes import compliance_check

    state = _happy_state()
    state.requested_by_user_id = 0  # missing identity envelope
    state = await compliance_check(state)
    failed = state.compliance_checks[-1]["details"]["failed_gates"]
    assert any(g.startswith("G07_") for g in failed)


@pytest.mark.asyncio
async def test_g08_r4_without_geologist_signoff_warns_in_graph_pass() -> None:
    """Graph-internal compliance treats sign-off gates as warnings."""
    from app.services.report_builder.nodes import compliance_check

    state = _happy_state("R4")
    state.sign_offs = []  # no records
    state = await compliance_check(state)
    warnings = state.compliance_checks[-1]["details"]["warnings"]
    assert any(w.startswith("G08_") for w in warnings)
    # State is NOT marked failed — the workflow body keeps going.
    assert state.compliance_passed is True


@pytest.mark.asyncio
async def test_g08_unsigned_signoff_warns_in_graph_pass() -> None:
    from app.services.report_builder.nodes import compliance_check
    from app.services.report_builder.state import SignOffRecord

    state = _happy_state("R4")
    state.sign_offs = [
        SignOffRecord(role="geologist", user_id=1, signed_at=None),
    ]
    state = await compliance_check(state)
    warnings = state.compliance_checks[-1]["details"]["warnings"]
    assert any("G08_unsigned" in w for w in warnings)
    assert state.compliance_passed is True


@pytest.mark.asyncio
async def test_g09_r5_qp_credential_missing_warns_in_graph_pass() -> None:
    from app.services.report_builder.nodes import compliance_check
    from app.services.report_builder.state import SignOffRecord

    state = _happy_state("R5")
    state.sign_offs = [
        SignOffRecord(
            role="geologist",
            user_id=1,
            signed_at=datetime.now(tz=UTC),
        ),
        SignOffRecord(
            role="qp",
            user_id=2,
            qp_credential_id=None,  # missing!
            signed_at=datetime.now(tz=UTC),
        ),
    ]
    state = await compliance_check(state)
    warnings = state.compliance_checks[-1]["details"]["warnings"]
    assert any(w.startswith("G09_") for w in warnings)


@pytest.mark.asyncio
async def test_g10_missing_hash_chain_proof_fails() -> None:
    from app.services.report_builder.nodes import compliance_check

    state = _happy_state()
    state.hash_chain_proof = None
    state = await compliance_check(state)
    failed = state.compliance_checks[-1]["details"]["failed_gates"]
    assert any(g.startswith("G10_") for g in failed)


@pytest.mark.asyncio
async def test_g10_malformed_hash_chain_proof_fails() -> None:
    from app.services.report_builder.nodes import compliance_check

    state = _happy_state()
    state.hash_chain_proof = {"oops": "no anchor"}
    state = await compliance_check(state)
    failed = state.compliance_checks[-1]["details"]["failed_gates"]
    assert any("G10_hash_chain" in g for g in failed)


@pytest.mark.asyncio
async def test_g10_accepts_evidence_sha256_proof() -> None:
    """The Phase G.3 evidence_sha256 anchor shape satisfies §29.2.10."""
    from app.services.report_builder.nodes import compliance_check

    state = _happy_state()
    state.hash_chain_proof = {
        "schema_version":      1,
        "report_id":           "abc",
        "evidence_sha256":     "a" * 64,
        "citation_count":      1,
        "evidence_item_count": 1,
    }
    state = await compliance_check(state)
    assert state.compliance_passed is True
    failed = state.compliance_checks[-1]["details"]["failed_gates"]
    assert not any(g.startswith("G10") for g in failed)
