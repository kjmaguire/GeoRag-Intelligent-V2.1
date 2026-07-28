"""Phase H4 Tier 2/3/4 UI router smoke tests.

conflicts.py coverage removed 2026-07-28 (task #31) — the router was deleted
(zero Laravel-side callers; the admin page that reached it was gone since the
reader-core trim).
"""
from __future__ import annotations

from app.routers import (
    audit_findings as audit_findings_router,
)
from app.routers import (
    what_changed as what_changed_router,
)


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


def test_cold_tier_archive_request_dry_run_default() -> None:
    from datetime import datetime

    from app.routers.audit_findings import ColdTierArchiveRequest
    req = ColdTierArchiveRequest(cutoff_before_iso=datetime(2026, 1, 1))
    assert req.dry_run is True
    assert req.archive_bucket == "audit-cold-tier"
