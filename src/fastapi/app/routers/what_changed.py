"""§9.9 / §20.2 What-Changed digest viewer endpoints (Phase H4 UI).

  GET /api/v1/admin/what-changed/runs
      Lists recent what_changed_detector audit anchors.

  GET /api/v1/admin/what-changed/{run_id}
      Returns the digest payload for one detection run.

Authentication: service-key.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.services.auth import verify_service_key


logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/v1/admin/what-changed",
    tags=["what-changed-digest"],
    dependencies=[Depends(verify_service_key)],
)


class WhatChangedRun(BaseModel):
    run_id: str
    workspace_id: str | None
    created_at: datetime
    payload: dict[str, Any]


class WhatChangedList(BaseModel):
    runs: list[WhatChangedRun]
    total: int


@router.get("/runs", response_model=WhatChangedList)
async def list_runs(limit: int = 50) -> WhatChangedList:
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )
    limit = max(1, min(limit, 500))

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text           AS run_id,
                   workspace_id::text AS workspace_id,
                   created_at         AS created_at,
                   payload            AS payload
              FROM audit.audit_ledger
             WHERE action_type = 'workspace.what_changed.detected'
             ORDER BY created_at DESC
             LIMIT $1
            """,
            limit,
        )

    out: list[WhatChangedRun] = []
    for r in rows:
        payload = r["payload"] or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:  # noqa: BLE001
                payload = {}
        out.append(WhatChangedRun(
            run_id=r["run_id"],
            workspace_id=r["workspace_id"],
            created_at=r["created_at"],
            payload=payload if isinstance(payload, dict) else {},
        ))
    return WhatChangedList(runs=out, total=len(out))


__all__ = ["router"]
