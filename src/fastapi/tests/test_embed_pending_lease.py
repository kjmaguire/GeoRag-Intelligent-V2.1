"""embed_pending_passages: two runs do not embed the same project at once.

The workflow's concurrency key is the workspace id for an inline dispatch
(ingest_pdf.persist) and the literal 'cron' for the fan-out, so a cron tick and an
inline run for the same project are different groups and ran side by side, each
reading the same unembedded passages and sending them to the embedder (billed per
text) and to Qdrant (Hatchet audit 2026-10, finding 14). A per-project advisory
lock on a direct connection makes them take turns; the loser skips the project and
the next */10 tick covers whatever landed since.

The lock itself needs a real Postgres (the ``integration`` tests); the loop's
handling of a busy project is checked with the lock faked.
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.hatchet_workflows import embed_pending_passages as mod

WS = "a0000000-0000-0000-0000-000000000001"


# ---------------------------------------------------------------------------
# The lock (real Postgres)
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_second_run_for_a_project_is_told_it_is_busy() -> None:
    key = f"embed_pending_passages:{WS}:{uuid4()}"
    async with mod._embed_lease(key) as first:
        assert first is True
        async with mod._embed_lease(key) as second:
            assert second is False, "two runs would embed the same project at once"
        async with mod._embed_lease(f"{key}-other-project") as other:
            assert other is True, "a different project must not be held up"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_lease_is_free_again_after_the_run_ends_even_if_it_failed() -> None:
    key = f"embed_pending_passages:{WS}:{uuid4()}"
    with pytest.raises(RuntimeError, match="embedder blew up"):
        async with mod._embed_lease(key) as held:
            assert held is True
            raise RuntimeError("embedder blew up")

    async with mod._embed_lease(key) as again:
        assert again is True, "a failed run left its project locked"


@pytest.mark.asyncio
async def test_a_lock_that_cannot_be_taken_fails_open(monkeypatch) -> None:
    """The lock guards against paying twice. Refusing to embed because the guard
    is unreachable would be the worse failure."""
    async def _down(*_a: object, **_k: object) -> None:
        raise ConnectionError("postgres unreachable")

    monkeypatch.setattr(mod.asyncpg, "connect", _down)

    async with mod._embed_lease("embed_pending_passages:any") as leased:
        assert leased is True


# ---------------------------------------------------------------------------
# The loop (lock faked)
# ---------------------------------------------------------------------------
class _FakeConn:
    async def close(self) -> None:
        return None


class _FakePool:
    def acquire(self) -> _FakePool:
        return self

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn()

    async def __aexit__(self, *_exc: object) -> bool:
        return False


@pytest.fixture
def sweep(monkeypatch):
    """`run` over one named project with everything but the embed call faked."""
    from app.hatchet_workflows import _progress as ingest_progress
    from app.services.ingest import orphan_sweep, passage_embedder

    embedded: list[str] = []
    busy: set[str] = set()

    async def _embed(*, workspace_id: str, project_id: str | None, **_k: object):
        embedded.append(str(project_id))
        return SimpleNamespace(
            passages_seen=3, passages_embedded=3, qdrant_points_upserted=0,
            passages_skipped=0, errors=[],
        )

    @contextlib.asynccontextmanager
    async def _lease(key: str):
        yield not any(b in key for b in busy)

    async def _connect(*_a: object, **_k: object) -> _FakeConn:
        return _FakeConn()

    async def _no_rows(*_a: object, **_k: object) -> list:
        return []

    async def _get_pool() -> _FakePool:
        return _FakePool()

    async def _no_orphans(_pool: object):
        return [], []

    monkeypatch.setattr(mod, "embed_pending_passages", _embed)
    monkeypatch.setattr(mod, "_embed_lease", _lease)
    monkeypatch.setattr(mod.asyncpg, "connect", _connect)
    monkeypatch.setattr(mod, "fetch_per_workspace", _no_rows)
    monkeypatch.setattr(ingest_progress, "get_pool", _get_pool)
    monkeypatch.setattr(orphan_sweep, "claim_and_record_recovery", _no_orphans)
    monkeypatch.setattr(passage_embedder, "load_embedding_model", lambda: None)

    async def _run(project_id: str):
        task = getattr(mod.run, "_fn", mod.run)
        return await task(
            mod.EmbedPendingPassagesInput(workspace_id=WS, project_id=project_id),
            SimpleNamespace(),
        )

    return SimpleNamespace(run=_run, embedded=embedded, busy=busy)


@pytest.mark.asyncio
async def test_a_project_another_run_is_embedding_is_left_to_that_run(sweep) -> None:
    project = str(uuid4())
    sweep.busy.add(project)

    out = await sweep.run(project)

    assert sweep.embedded == [], "the project was embedded by two runs at once"
    assert out.projects_skipped_busy == 1
    assert out.total_embedded == 0


@pytest.mark.asyncio
async def test_a_free_project_is_embedded_as_before(sweep) -> None:
    project = str(uuid4())

    out = await sweep.run(project)

    assert sweep.embedded == [project]
    assert out.projects_skipped_busy == 0
    assert out.total_embedded == 3
