"""A document_passage evidence row whose passage was deleted is a 410, not a 500.

§04e, SME-approved (Kyle, 2026-09-29): ``silver.evidence_items.passage_id``
is ``ON DELETE SET NULL`` (it was RESTRICT), so deleting or re-ingesting a
document is no longer blocked by the evidence that cited it. The evidence
row survives as a tombstone — ``evidence_type = 'document_passage'`` with
every ref NULL — and the reader must say "the source is gone", not crash.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.routers.evidence import _assemble_passage


@pytest.mark.asyncio
async def test_a_tombstoned_passage_is_gone_not_a_server_error() -> None:
    row = {
        "evidence_id": str(uuid.uuid4()),
        "evidence_type": "document_passage",
        "passage_id": None,
        "source_uri": "bronze/ws/report.pdf",
    }

    with pytest.raises(HTTPException) as exc:
        await _assemble_passage(row, pg_pool=object(), workspace_id=uuid.uuid4())

    assert exc.value.status_code == 410
    assert exc.value.detail == "evidence_source_deleted"
