"""SME content seeder runner — doc-phase 123.

Reads a content module (e.g. `athabasca_uranium`), validates that
it's fully populated, and idempotently lands one row into
`targeting.target_models` + one new row into
`targeting.target_model_versions` with `is_active=true`.

Each seed run also emits an `audit.audit_ledger` entry via
`app.audit.emit_audit` so the SME pass leaves a chain anchor — same
pattern the §21 decision intelligence uses.

Re-running with the same slug:
- target_models: find-or-update by slug
- target_model_versions: deactivates any prior active version,
  inserts a new version row with the incremented version number +
  is_active=true. (Per master plan §18.3 "the two approaches coexist"
  — every iteration is a new version, full history retained.)
"""
from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg

from app.audit import emit_audit


@dataclass(frozen=True, slots=True)
class SmeSeedResult:
    slug: str
    target_model_id: UUID
    new_version_id: UUID
    new_version_number: int
    audit_ledger_id: UUID
    was_created: bool        # True on first-time seed; False on update
    deactivated_versions: int  # how many prior versions had is_active flipped to false


class SmeContentNotReadyError(Exception):
    """Raised when a content module still has empty / TODO blocks."""

    def __init__(self, module_path: str, blockers: list[str]):
        self.module_path = module_path
        self.blockers = blockers
        msg = (
            f"SME content module '{module_path}' is not fully populated. "
            f"Blockers ({len(blockers)}):\n  - "
            + "\n  - ".join(blockers)
        )
        super().__init__(msg)


async def seed_deposit_model_from_module(
    conn: asyncpg.Connection,
    *,
    module_path: str,
    initiated_by_user_id: int,
    activate_new_version: bool = True,
) -> SmeSeedResult:
    """Idempotently land a deposit-model row + a new active version.

    Args:
        conn: asyncpg Connection. Function manages its own transaction.
        module_path: dotted path to a content module under
            `app.services.target_recommendation.sme_content` — e.g.
            `app.services.target_recommendation.sme_content.athabasca_uranium`.
        initiated_by_user_id: public.users.id of the geologist running the
            seed (recorded as the audit ledger actor).
        activate_new_version: when True (default), deactivates any prior
            active versions for this model + flips the new version to
            is_active=true. Set False to land the version inactive for
            A/B comparison testing.

    Returns:
        SmeSeedResult.

    Raises:
        SmeContentNotReadyError when the content module reports blockers.
    """
    # 1. Import + validate content module
    try:
        mod = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        raise ValueError(
            f"Cannot import SME content module '{module_path}': {exc}"
        ) from exc

    if not hasattr(mod, "is_populated") or not hasattr(mod, "get_content"):
        raise ValueError(
            f"Module '{module_path}' is missing required functions "
            f"`is_populated()` and/or `get_content()`."
        )

    # Re-import to pick up any in-process edits during dev. Skip the
    # reload when the module was dynamically created (no __spec__) —
    # importlib.reload() raises ModuleNotFoundError on those.
    if getattr(mod, "__spec__", None) is not None:
        mod = importlib.reload(mod)
    ready, blockers = mod.is_populated()
    if not ready:
        raise SmeContentNotReadyError(module_path, blockers)

    content = mod.get_content()

    # 2. Inside one transaction: upsert target_models, deactivate prior
    # versions if requested, insert new version, emit audit row.
    async with conn.transaction():
        # ---- 2a. Upsert target_models ----
        existing_id = await conn.fetchval(
            "SELECT target_model_id FROM targeting.target_models WHERE slug = $1",
            content["slug"],
        )
        was_created = existing_id is None

        if was_created:
            target_model_id = await conn.fetchval(
                """
                INSERT INTO targeting.target_models (
                    slug, display_name, commodity_primary, commodities_secondary,
                    attributes_payload, positive_indicators, negative_indicators,
                    analogues_payload, recommended_next_data
                )
                VALUES (
                    $1, $2, $3, $4::text[], $5::jsonb, $6::jsonb, $7::jsonb,
                    $8::jsonb, $9::jsonb
                )
                RETURNING target_model_id
                """,
                content["slug"],
                content["display_name"],
                content["commodity_primary"],
                list(content["commodities_secondary"]),
                json.dumps(content["attributes_payload"]),
                json.dumps(content["positive_indicators"]),
                json.dumps(content["negative_indicators"]),
                json.dumps(content["analogues_payload"]),
                json.dumps(content["recommended_next_data"]),
            )
        else:
            target_model_id = existing_id
            await conn.execute(
                """
                UPDATE targeting.target_models SET
                    display_name = $2,
                    commodity_primary = $3,
                    commodities_secondary = $4::text[],
                    attributes_payload = $5::jsonb,
                    positive_indicators = $6::jsonb,
                    negative_indicators = $7::jsonb,
                    analogues_payload = $8::jsonb,
                    recommended_next_data = $9::jsonb
                WHERE target_model_id = $1::uuid
                """,
                str(target_model_id),
                content["display_name"],
                content["commodity_primary"],
                list(content["commodities_secondary"]),
                json.dumps(content["attributes_payload"]),
                json.dumps(content["positive_indicators"]),
                json.dumps(content["negative_indicators"]),
                json.dumps(content["analogues_payload"]),
                json.dumps(content["recommended_next_data"]),
            )

        # ---- 2b. Deactivate prior active versions if requested ----
        deactivated_versions = 0
        if activate_new_version:
            deactivated_versions = await conn.fetchval(
                """
                WITH affected AS (
                    UPDATE targeting.target_model_versions
                    SET is_active = false
                    WHERE target_model_id = $1::uuid AND is_active = true
                    RETURNING 1
                )
                SELECT count(*) FROM affected
                """,
                str(target_model_id),
            )

        # ---- 2c. Compute next version number ----
        new_version_number = (await conn.fetchval(
            """
            SELECT COALESCE(MAX(version), 0) + 1
            FROM targeting.target_model_versions
            WHERE target_model_id = $1::uuid
            """,
            str(target_model_id),
        )) or 1

        # ---- 2d. Insert new version ----
        new_version_id = await conn.fetchval(
            """
            INSERT INTO targeting.target_model_versions (
                target_model_id, version, scoring_kind, factor_weights,
                constraint_payload, is_active
            )
            VALUES ($1::uuid, $2, $3, $4::jsonb, $5::jsonb, $6)
            RETURNING version_id
            """,
            str(target_model_id),
            new_version_number,
            "weighted",
            json.dumps(content["scoring_weights"]),
            json.dumps({}),  # constraint_payload — extend in v2
            activate_new_version,
        )

        # ---- 2e. Audit ledger anchor ----
        ledger_entry = await emit_audit(
            conn,
            action_type="deposit_model.seed",
            workspace_id=None,  # global reference data
            actor_id=initiated_by_user_id,
            actor_kind="user",
            target_schema="targeting",
            target_table="target_models",
            target_id=str(target_model_id),
            payload={
                "slug": content["slug"],
                "version_number": new_version_number,
                "activated": activate_new_version,
                "deactivated_prior_versions": int(deactivated_versions or 0),
                "scoring_weights": content["scoring_weights"],
                "host_rock_count": len(content["attributes_payload"]["host_rocks"]),
                "structure_count": len(content["attributes_payload"]["structures"]),
                "alteration_count": len(content["attributes_payload"]["alteration"]),
                "pathfinder_element_count": len(
                    content["attributes_payload"]["geochemistry"]["pathfinder_elements"]
                ),
                "analogue_count": len(content["analogues_payload"]),
            },
        )

    return SmeSeedResult(
        slug=content["slug"],
        target_model_id=target_model_id,
        new_version_id=new_version_id,
        new_version_number=new_version_number,
        audit_ledger_id=ledger_entry.id,
        was_created=was_created,
        deactivated_versions=int(deactivated_versions or 0),
    )


__all__ = [
    "SmeContentNotReadyError",
    "SmeSeedResult",
    "seed_deposit_model_from_module",
]
