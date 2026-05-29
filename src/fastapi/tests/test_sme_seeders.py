"""Live tests for the SME deposit-model seeder (doc-phase 123).

Two test groups:

1. **Guard tests** — the seeder MUST refuse to run when the content
   module still has TODO blocks. This is the §8.3 R5 sign-off
   protection: never land half-curated reference data.

2. **End-to-end tests** — use a synthetic content module that's
   fully populated; verify the row + version + audit anchor land.
"""
from __future__ import annotations

import os
import sys
import types
from uuid import uuid4

import asyncpg
import pytest

from app.services.target_recommendation.sme_content import (
    SmeSeedResult,
    seed_deposit_model_from_module,
)
from app.services.target_recommendation.sme_content.seed_runner import (
    SmeContentNotReadyError,
)


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@pytest.fixture
async def conn():
    c = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
async def synthetic_user(conn):
    email = f"sme-seeder-test-{uuid4()}@example.com"
    user_id = await conn.fetchval(
        "INSERT INTO public.users (name, email, password) VALUES ($1,$2,$3) RETURNING id",
        "SME Seeder Test User", email, "test-hash",
    )
    try:
        yield user_id
    finally:
        await conn.execute("DELETE FROM public.users WHERE id = $1", user_id)


def _install_fake_module(name: str, *, is_populated_returns: tuple[bool, list[str]],
                          content_returns: dict) -> None:
    """Inject a fake SME content module into sys.modules for testing."""
    mod = types.ModuleType(name)
    mod.is_populated = lambda: is_populated_returns
    mod.get_content = lambda: content_returns
    sys.modules[name] = mod


def _full_synthetic_content(slug: str) -> dict:
    """Return a complete content dict — passes is_populated() blocker checks."""
    return {
        "slug": slug,
        "display_name": f"Test {slug}",
        "commodity_primary": "U",
        "commodities_secondary": ["Ni"],
        "attributes_payload": {
            "host_rocks": ["test sandstone"],
            "structures": ["test fault"],
            "alteration": ["test clay"],
            "geochemistry": {
                "pathfinder_elements": ["U", "Pb"],
                "element_ratios": ["Pb/U > 1"],
                "anomaly_thresholds": {"U_ppm_min": 100},
            },
            "geophysics": {
                "magnetic_signature": "low",
                "radiometric_signature": "high U surface",
                "gravity_signature": "",
                "em_signature": "conductive",
                "ip_resistivity_signature": "low resistivity",
            },
            "tectonic_setting": ["intracratonic basin"],
        },
        "positive_indicators": ["unconformity + fault"],
        "negative_indicators": ["thick unweathered cover"],
        "analogues_payload": [
            {"name": "Test Analogue", "location": "Test Basin"},
        ],
        "recommended_next_data": [
            {"kind": "em_survey", "scope": "AOI"},
        ],
        "scoring_weights": {
            "alteration": 0.25, "structural": 0.25, "geochemistry": 0.20,
            "proximity": 0.10, "geophysics": 0.10, "analogue": 0.10,
        },
    }


# ---------------------------------------------------------------------------
# Guard tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refuses_when_content_not_populated(conn, synthetic_user):
    """Module reporting blockers raises SmeContentNotReadyError + writes nothing."""
    mod_name = f"app.services.target_recommendation.sme_content.test_unpopulated_{uuid4().hex[:8]}"
    _install_fake_module(
        mod_name,
        is_populated_returns=(False, ["HOST_ROCKS is empty", "ANALOGUES is empty"]),
        content_returns=_full_synthetic_content("should_never_be_seeded"),
    )
    try:
        with pytest.raises(SmeContentNotReadyError) as excinfo:
            await seed_deposit_model_from_module(
                conn,
                module_path=mod_name,
                initiated_by_user_id=synthetic_user,
            )
        assert "HOST_ROCKS is empty" in str(excinfo.value)
        assert "ANALOGUES is empty" in str(excinfo.value)

        # Nothing landed
        count = await conn.fetchval(
            "SELECT count(*) FROM targeting.target_models WHERE slug = $1",
            "should_never_be_seeded",
        )
        assert count == 0
    finally:
        sys.modules.pop(mod_name, None)


@pytest.mark.asyncio
async def test_missing_module_raises_clear_error(conn, synthetic_user):
    """A non-existent module path raises ValueError immediately."""
    with pytest.raises(ValueError, match="Cannot import SME content module"):
        await seed_deposit_model_from_module(
            conn,
            module_path="app.services.target_recommendation.sme_content.does_not_exist",
            initiated_by_user_id=synthetic_user,
        )


# ---------------------------------------------------------------------------
# End-to-end tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_seed_creates_model_and_active_version(conn, synthetic_user):
    """First seed creates target_models + target_model_versions + audit row."""
    slug = f"test_seed_{uuid4().hex[:8]}"
    mod_name = f"app.services.target_recommendation.sme_content.{slug}"
    _install_fake_module(
        mod_name,
        is_populated_returns=(True, []),
        content_returns=_full_synthetic_content(slug),
    )
    try:
        result = await seed_deposit_model_from_module(
            conn,
            module_path=mod_name,
            initiated_by_user_id=synthetic_user,
            activate_new_version=True,
        )
        assert isinstance(result, SmeSeedResult)
        assert result.was_created is True
        assert result.new_version_number == 1
        assert result.deactivated_versions == 0

        # Verify the model row
        row = await conn.fetchrow(
            "SELECT slug, display_name, commodity_primary FROM targeting.target_models "
            "WHERE target_model_id = $1::uuid",
            str(result.target_model_id),
        )
        assert row["slug"] == slug
        assert row["display_name"] == f"Test {slug}"
        assert row["commodity_primary"] == "U"

        # Verify the active version
        v = await conn.fetchrow(
            "SELECT version, is_active, scoring_kind FROM targeting.target_model_versions "
            "WHERE version_id = $1::uuid",
            str(result.new_version_id),
        )
        assert v["version"] == 1
        assert v["is_active"] is True
        assert v["scoring_kind"] == "weighted"

        # Verify the audit ledger entry
        audit = await conn.fetchrow(
            "SELECT action_type, target_schema, target_table, target_id "
            "FROM audit.audit_ledger WHERE id = $1::uuid",
            str(result.audit_ledger_id),
        )
        assert audit["action_type"] == "deposit_model.seed"
        assert audit["target_schema"] == "targeting"
        assert audit["target_table"] == "target_models"
        assert audit["target_id"] == str(result.target_model_id)
    finally:
        await conn.execute(
            "DELETE FROM targeting.target_models WHERE slug = $1", slug
        )
        sys.modules.pop(mod_name, None)


@pytest.mark.asyncio
async def test_reseed_updates_model_and_creates_new_version(
    conn, synthetic_user
):
    """Re-running the seeder upserts the model + creates v2 + deactivates v1."""
    slug = f"test_reseed_{uuid4().hex[:8]}"
    mod_name = f"app.services.target_recommendation.sme_content.{slug}"
    _install_fake_module(
        mod_name,
        is_populated_returns=(True, []),
        content_returns=_full_synthetic_content(slug),
    )
    try:
        # First seed → version 1
        r1 = await seed_deposit_model_from_module(
            conn, module_path=mod_name, initiated_by_user_id=synthetic_user,
        )
        assert r1.new_version_number == 1
        assert r1.was_created is True

        # Re-seed → version 2, prior deactivated
        r2 = await seed_deposit_model_from_module(
            conn, module_path=mod_name, initiated_by_user_id=synthetic_user,
        )
        assert r2.new_version_number == 2
        assert r2.was_created is False
        assert r2.deactivated_versions == 1
        assert r2.target_model_id == r1.target_model_id

        # v1 is now inactive, v2 is active
        v1_active = await conn.fetchval(
            "SELECT is_active FROM targeting.target_model_versions "
            "WHERE version_id = $1::uuid",
            str(r1.new_version_id),
        )
        v2_active = await conn.fetchval(
            "SELECT is_active FROM targeting.target_model_versions "
            "WHERE version_id = $1::uuid",
            str(r2.new_version_id),
        )
        assert v1_active is False
        assert v2_active is True
    finally:
        await conn.execute(
            "DELETE FROM targeting.target_models WHERE slug = $1", slug
        )
        sys.modules.pop(mod_name, None)


@pytest.mark.asyncio
async def test_inactive_seed_does_not_deactivate_prior(conn, synthetic_user):
    """activate_new_version=False leaves prior active version untouched."""
    slug = f"test_inactive_seed_{uuid4().hex[:8]}"
    mod_name = f"app.services.target_recommendation.sme_content.{slug}"
    _install_fake_module(
        mod_name,
        is_populated_returns=(True, []),
        content_returns=_full_synthetic_content(slug),
    )
    try:
        r1 = await seed_deposit_model_from_module(
            conn, module_path=mod_name, initiated_by_user_id=synthetic_user,
            activate_new_version=True,
        )
        # Re-seed inactive — prior remains active
        r2 = await seed_deposit_model_from_module(
            conn, module_path=mod_name, initiated_by_user_id=synthetic_user,
            activate_new_version=False,
        )
        assert r2.deactivated_versions == 0

        v1_active = await conn.fetchval(
            "SELECT is_active FROM targeting.target_model_versions "
            "WHERE version_id = $1::uuid",
            str(r1.new_version_id),
        )
        v2_active = await conn.fetchval(
            "SELECT is_active FROM targeting.target_model_versions "
            "WHERE version_id = $1::uuid",
            str(r2.new_version_id),
        )
        assert v1_active is True
        assert v2_active is False
    finally:
        await conn.execute(
            "DELETE FROM targeting.target_models WHERE slug = $1", slug
        )
        sys.modules.pop(mod_name, None)


@pytest.mark.asyncio
async def test_athabasca_uranium_module_currently_blocked(conn, synthetic_user):
    """The real Athabasca module ships with TODO placeholders — must
    refuse to seed until Kyle fills it in.

    This test is the protective rail that catches an accidental
    deployment with un-populated content."""
    with pytest.raises(SmeContentNotReadyError):
        await seed_deposit_model_from_module(
            conn,
            module_path=(
                "app.services.target_recommendation.sme_content.athabasca_uranium"
            ),
            initiated_by_user_id=synthetic_user,
        )
