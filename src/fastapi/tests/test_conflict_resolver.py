"""§7.4 Conflict Resolver tests (Phase H4)."""
from __future__ import annotations

from app.agents.phase7.conflict_resolver import conflict_resolver


def _claim(
    cid: str, text: str,
    *, validated: bool = True,
    evidence: list[dict] | None = None,
) -> dict:
    return {
        "claim_id":  cid,
        "text":      text,
        "validated": validated,
        "evidence":  evidence or [],
    }


def _run(claims, *, workspace_data_version=None, section_id="sec-1"):
    """Invoke the wrapped agent (skipping the decorator scaffolding)."""
    import asyncio
    inner = getattr(conflict_resolver, "__wrapped__", conflict_resolver)
    return asyncio.run(inner(
        ctx=None,
        workspace_id="ws-1",
        section_id=section_id,
        claims=claims,
        workspace_data_version=workspace_data_version,
    ))


def test_no_conflicts_when_claims_agree() -> None:
    claims = [
        _claim("c1", "The project has 63 drillholes.",
               evidence=[{"source_chunk_id": "e1", "is_stale": False}]),
        _claim("c2", "63 drillholes were recorded in the dataset.",
               evidence=[{"source_chunk_id": "e2", "is_stale": False}]),
    ]
    result = _run(claims)
    assert result["all_resolved"] is True
    assert result["conflicts"] == []


def test_value_mismatch_flagged() -> None:
    claims = [
        _claim("c1", "Total depth of hole PLS-22-08 is 339 metres.",
               evidence=[{"source_chunk_id": "e1", "is_stale": False}]),
        _claim("c2", "Total depth of hole PLS-22-08 is 510 metres.",
               evidence=[{"source_chunk_id": "e2", "is_stale": False}]),
    ]
    result = _run(claims)
    assert any(c["kind"] == "value_mismatch" for c in result["conflicts"])
    vm = next(c for c in result["conflicts"] if c["kind"] == "value_mismatch")
    assert set(vm["claim_ids"]) == {"c1", "c2"}


def test_value_within_tolerance_not_flagged() -> None:
    """Values within the 1% / 0.1 tolerance are NOT flagged as mismatch."""
    claims = [
        _claim("c1", "Total depth 339.9 metres.",
               evidence=[{"source_chunk_id": "e1", "is_stale": False}]),
        _claim("c2", "Total depth 339.95 metres.",  # within 0.1
               evidence=[{"source_chunk_id": "e2", "is_stale": False}]),
    ]
    result = _run(claims)
    assert not any(c["kind"] == "value_mismatch"
                   for c in result["conflicts"])


def test_freshness_drift_flagged_when_evidence_stale() -> None:
    claims = [
        _claim("c1", "The project covers 1234 hectares.",
               evidence=[
                   {"source_chunk_id": "stale-e1", "is_stale": True},
               ]),
    ]
    result = _run(claims, workspace_data_version=42)
    fresh_conflicts = [c for c in result["conflicts"] if c["kind"] == "freshness"]
    assert len(fresh_conflicts) == 1
    assert "stale-e1" in fresh_conflicts[0]["evidence_ids"]


def test_freshness_drift_skipped_when_no_data_version() -> None:
    """No data_version cut-over → freshness check is disabled."""
    claims = [
        _claim("c1", "Old claim.",
               evidence=[{"source_chunk_id": "stale-e1", "is_stale": True}]),
    ]
    result = _run(claims, workspace_data_version=None)
    assert not any(c["kind"] == "freshness" for c in result["conflicts"])


def test_missing_provenance_flagged_as_manual_review() -> None:
    """A validated claim with no evidence rows is a §04i Layer 5
    violation that routes to manual review."""
    claims = [
        _claim("c1", "The project covers 5000 hectares.",
               validated=True, evidence=[]),
    ]
    result = _run(claims)
    mp = [c for c in result["conflicts"] if c["kind"] == "missing_provenance"]
    assert len(mp) == 1
    assert mp[0]["resolution"] == "manual_review"
    assert mp[0]["claim_ids"] == ["c1"]


def test_unvalidated_claim_with_no_evidence_not_flagged() -> None:
    """Claims that aren't validated yet shouldn't fire the missing-
    provenance rule (the §04i pipeline catches them upstream)."""
    claims = [
        _claim("c1", "Tentative claim awaiting review.",
               validated=False, evidence=[]),
    ]
    result = _run(claims)
    assert not any(c["kind"] == "missing_provenance"
                   for c in result["conflicts"])


def test_all_resolved_false_when_manual_review_required() -> None:
    """Manual-review conflicts count as unresolved."""
    claims = [
        _claim("c1", "Some claim.", validated=True, evidence=[]),
    ]
    result = _run(claims)
    assert result["all_resolved"] is False


def test_all_resolved_true_when_only_auto_disclose() -> None:
    """Value mismatches resolve to auto_disclose → all_resolved=True."""
    claims = [
        _claim("c1", "Depth 100 m.",
               evidence=[{"source_chunk_id": "e1", "is_stale": False}]),
        _claim("c2", "Depth 200 m.",
               evidence=[{"source_chunk_id": "e2", "is_stale": False}]),
    ]
    result = _run(claims)
    assert result["all_resolved"] is True


def test_summary_includes_kind_counts() -> None:
    claims = [
        _claim("c1", "Depth 100 m.",
               evidence=[{"source_chunk_id": "e1", "is_stale": False}]),
        _claim("c2", "Depth 200 m.",
               evidence=[{"source_chunk_id": "e2", "is_stale": True}]),
    ]
    result = _run(claims, workspace_data_version=10)
    assert "value_mismatch=" in result["summary"]
    assert "freshness=" in result["summary"]


def test_evidence_ids_unique_in_value_mismatch_block() -> None:
    """When two claims cite overlapping evidence, the conflict's
    evidence_ids list deduplicates."""
    claims = [
        _claim("c1", "Depth 100 m.",
               evidence=[
                   {"source_chunk_id": "shared", "is_stale": False},
                   {"source_chunk_id": "e1", "is_stale": False},
               ]),
        _claim("c2", "Depth 200 m.",
               evidence=[
                   {"source_chunk_id": "shared", "is_stale": False},
                   {"source_chunk_id": "e2", "is_stale": False},
               ]),
    ]
    result = _run(claims)
    vm = next(c for c in result["conflicts"] if c["kind"] == "value_mismatch")
    # "shared" appears once, not twice
    assert vm["evidence_ids"].count("shared") == 1


def test_three_way_conflict_produces_three_pairs() -> None:
    """N claims with N distinct values → C(N, 2) conflict pairs."""
    claims = [
        _claim(f"c{i}", f"Depth {(i+1)*100} m.",
               evidence=[{"source_chunk_id": f"e{i}", "is_stale": False}])
        for i in range(3)
    ]
    result = _run(claims)
    vm = [c for c in result["conflicts"] if c["kind"] == "value_mismatch"]
    # 3-choose-2 = 3 pairs
    assert len(vm) == 3
