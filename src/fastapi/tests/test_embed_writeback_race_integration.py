"""The embed writeback no longer undoes an enrichment reset, against a real Postgres (audit finding 19).

The race, in time order:

  T0  embed_pending reads a passage (embedding_id NULL, contextualized_content
      NULL) and starts encoding its bare text;
  T1  context_enricher writes contextualized_content and clears embedding_id so
      the sweep re-embeds the ENRICHED text;
  T2  embed_pending finishes and sets embedding_id = <point id>.

The unconditional UPDATE at T2 stamped the row "embedded" with a vector built
from the old content, so the enrichment was never embedded. The guard
(``passage_embedder._WRITEBACK_SQL``) matches nothing at T2, leaves
embedding_id NULL, and the next sweep encodes the new content.
"""
from __future__ import annotations

# ruff: noqa: F811 - the `project` fixture is imported, then named as a parameter
import uuid

import pytest

from app.services.ingest.passage_embedder import _WRITEBACK_SQL

# Importing the sibling module also applies its module-level skip when no
# Postgres is configured, and brings the `project` fixture along.
from tests.test_ingest_constraint_rows_integration import (  # noqa: F401 - `project` is a fixture
    _Fixture,
    project,
)

pytestmark = pytest.mark.integration

#: The statement context_enricher runs (pinned by test_context_enrichment_reachable).
_ENRICH_SQL = (
    "UPDATE silver.document_passages "
    "   SET contextualized_content = $1, embedding_id = NULL, updated_at = NOW() "
    " WHERE passage_id = $2::uuid"
)


@pytest.fixture
async def passage(project: _Fixture):
    passage_id = str(uuid.uuid4())
    await project.conn.execute(
        "INSERT INTO silver.document_passages "
        "  (passage_id, workspace_id, revision_number, text, text_hash, ordinal) "
        "VALUES ($1::uuid, $2::uuid, 1, 'passage text', $3, 0)",
        passage_id, project.workspace_id, "a" * 64,
    )
    try:
        yield project, passage_id
    finally:
        await project.conn.execute(
            "DELETE FROM silver.document_passages WHERE passage_id = $1::uuid", passage_id,
        )


async def _row(project: _Fixture, passage_id: str):
    return await project.conn.fetchrow(
        "SELECT embedding_id, contextualized_content FROM silver.document_passages "
        "WHERE passage_id = $1::uuid", passage_id,
    )


async def test_an_unchanged_passage_is_marked_embedded(passage) -> None:
    project, passage_id = passage

    await project.conn.executemany(_WRITEBACK_SQL, [("point-1", passage_id, None)])

    assert (await _row(project, passage_id))["embedding_id"] == "point-1"


async def test_an_enriched_passage_whose_content_was_encoded_is_marked_embedded(passage) -> None:
    """IS NOT DISTINCT FROM matches equal non-NULL text, not only NULL = NULL."""
    project, passage_id = passage
    await project.conn.execute(_ENRICH_SQL, "Header. passage text", passage_id)

    await project.conn.executemany(
        _WRITEBACK_SQL, [("point-2", passage_id, "Header. passage text")],
    )

    assert (await _row(project, passage_id))["embedding_id"] == "point-2"


async def test_a_writeback_after_an_enrichment_reset_does_not_stick(passage) -> None:
    project, passage_id = passage
    # T0: the embedder read contextualized_content = NULL.  T1: the enricher runs.
    await project.conn.execute(_ENRICH_SQL, "Header. passage text", passage_id)

    # T2: the embedder's batch lands, carrying the content it encoded (NULL).
    await project.conn.executemany(_WRITEBACK_SQL, [("point-old", passage_id, None)])

    row = await _row(project, passage_id)
    assert row["embedding_id"] is None                      # still pending: re-embedded next sweep
    assert row["contextualized_content"] == "Header. passage text"

    # The next sweep reads the enriched content and its writeback sticks.
    await project.conn.executemany(
        _WRITEBACK_SQL, [("point-new", passage_id, "Header. passage text")],
    )
    assert (await _row(project, passage_id))["embedding_id"] == "point-new"
