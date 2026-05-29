"""Phase H4 Tier 2/3/4 UI router smoke tests."""
from __future__ import annotations

import pytest

from app.routers import (
    audit_findings as audit_findings_router,
    conflicts as conflicts_router,
    what_changed as what_changed_router,
)


def test_conflicts_router_mounted() -> None:
    assert conflicts_router.router.prefix == "/api/v1/admin/conflicts"


def test_audit_findings_router_mounted() -> None:
    assert audit_findings_router.router.prefix == "/api/v1/admin/audit"


def test_what_changed_router_mounted() -> None:
    assert what_changed_router.router.prefix == "/api/v1/admin/what-changed"


def test_tenant_isolation_finding_model_schema_alias() -> None:
    """schema_name field round-trips to "schema" via alias."""
    from app.routers.audit_findings import TenantIsolationFinding
    f = TenantIsolationFinding(schema_name="silver", table="x", gate="rls_enabled", detail="t")
    dump = f.model_dump(by_alias=True)
    assert dump == {"schema": "silver", "table": "x", "gate": "rls_enabled", "detail": "t"}


def test_run_resolver_request_requires_claims() -> None:
    from app.routers.conflicts import RunResolverRequest, ClaimInput
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        RunResolverRequest(
            workspace_id="11111111-1111-1111-1111-111111111111",
            claims=[],
        )


def test_run_resolver_request_accepts_minimal_claim() -> None:
    from app.routers.conflicts import RunResolverRequest, ClaimInput
    req = RunResolverRequest(
        workspace_id="11111111-1111-1111-1111-111111111111",
        claims=[ClaimInput(claim_id="c1", text="some claim")],
    )
    assert req.claims[0].validated is True
    assert req.section_id == "test-bench"


def test_cold_tier_archive_request_dry_run_default() -> None:
    from app.routers.audit_findings import ColdTierArchiveRequest
    from datetime import datetime
    req = ColdTierArchiveRequest(cutoff_before_iso=datetime(2026, 1, 1))
    assert req.dry_run is True
    assert req.archive_bucket == "audit-cold-tier"
