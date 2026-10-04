"""scripts/reset_embeddings_for_reencode.py ``--all`` (ADR-0025 migration step 4).

The default mode only touches rows with ``contextualized_content IS NOT NULL``
and the other re-embed tool skips page-image points by design, so after a
MODEL change neither clears everything. A collection holding two vector spaces
still returns results, ranked by meaningless cosines (ADR-0025 gotcha 2), so
``--all`` has to leave nothing behind: every Qdrant point, every passage, text
and image alike -- and it is destructive, so it asks first.

No database, no Qdrant: both are fakes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "reset_embeddings_for_reencode.py"
pytestmark = pytest.mark.skipif(
    not _SCRIPT.exists(), reason="repo-root scripts/ is not mounted in this test container"
)


@pytest.fixture
def reset(monkeypatch):
    spec = importlib.util.spec_from_file_location("reset_embeddings_under_test", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DRY_RUN", False)
    monkeypatch.setattr(module, "BATCH_SIZE", 2)
    return module


class _Pg:
    def __init__(self, *, total=5, with_id=4, images=1, left_after=0) -> None:
        self.total, self.with_id, self.images, self.left_after = total, with_id, images, left_after
        self.executed: list[str] = []
        self.closed = False
        self.events: list[str] = []

    async def fetchval(self, sql: str, *args: Any) -> int:
        if "modality = 'image'" in sql:
            return self.images
        if "embedding_id IS NOT NULL" in sql:
            # First call (before) vs the post-UPDATE verification.
            return self.with_id if not self.executed else self.left_after
        return self.total

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append(sql)
        self.events.append("pg_update")
        return f"UPDATE {self.with_id}"

    async def close(self) -> None:
        self.closed = True


class _Qdrant:
    def __init__(self, ids: list[int], events: list[str]) -> None:
        self.ids = list(ids)
        self.events = events
        self.deleted: list[Any] = []
        self.closed = False

    async def scroll(self, *, collection_name, limit, with_payload, with_vectors, offset=None):
        assert (with_payload, with_vectors) == (False, False)
        assert offset is None, "the script re-scrolls from the start after each delete"
        page = self.ids[:limit]
        return [SimpleNamespace(id=i) for i in page], None

    async def delete(self, *, collection_name, points_selector, wait):
        self.deleted.extend(points_selector)
        self.events.append("qdrant_delete")
        self.ids = [i for i in self.ids if i not in points_selector]

    async def count(self, *, collection_name, exact):
        return SimpleNamespace(count=len(self.ids))

    async def close(self) -> None:
        self.closed = True


def _wire(reset, monkeypatch, pg: _Pg, ids: list[int]) -> _Qdrant:
    qdrant = _Qdrant(ids, pg.events)
    monkeypatch.setattr(reset, "_qdrant_client", lambda: qdrant)
    return qdrant


# -- the flags ---------------------------------------------------------------


def test_the_flags_parse(reset) -> None:
    assert reset._parse_args([]).reset_everything is False
    args = reset._parse_args(["--all", "--yes"])
    assert (args.reset_everything, args.yes) == (True, True)


# -- the confirmation ----------------------------------------------------------


def test_yes_skips_the_prompt(reset) -> None:
    assert reset._confirm_reset_all(5, 1, assume_yes=True, dry_run=False) is True


def test_a_non_interactive_run_without_yes_is_refused(reset, monkeypatch, caplog) -> None:
    """`docker exec` without -t and ECS exec have no terminal; waiting on input
    would hang, and guessing "yes" would delete the corpus."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert reset._confirm_reset_all(5, 1, assume_yes=False, dry_run=False) is False
    assert "--yes" in caplog.text


def test_the_wrong_phrase_is_refused_and_the_right_one_accepted(reset, monkeypatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert reset._confirm_reset_all(5, 1, assume_yes=False, dry_run=False) is False
    monkeypatch.setattr("builtins.input", lambda _prompt: reset.CONFIRM_PHRASE)
    assert reset._confirm_reset_all(5, 1, assume_yes=False, dry_run=False) is True


def test_the_warning_names_the_snapshot_and_the_degraded_window(reset, caplog) -> None:
    reset._confirm_reset_all(5, 1, assume_yes=True, dry_run=False)
    assert "snapshot" in caplog.text and "degraded" in caplog.text
    assert "modality='image'" in caplog.text


# -- the reset itself -----------------------------------------------------------


@pytest.mark.asyncio
async def test_all_deletes_every_point_then_nulls_every_passage(reset, monkeypatch) -> None:
    pg = _Pg()
    qdrant = _wire(reset, monkeypatch, pg, [1, 2, 3, 4, 5])

    await reset.reset_all(pg, assume_yes=True)

    assert sorted(qdrant.deleted) == [1, 2, 3, 4, 5], "every point, found by scrolling Qdrant"
    assert qdrant.ids == []
    (sql,) = pg.executed
    assert "embedding_id = NULL" in sql
    assert "contextualized_content" not in sql, "--all must not be limited to enriched rows"
    assert "modality" not in sql, "text and image passages alike"
    # Qdrant first: the reverse order, interrupted, leaves passages marked
    # "to embed" beside points still serving the old space.
    assert pg.events.index("qdrant_delete") < pg.events.index("pg_update")
    assert qdrant.closed and pg.closed


@pytest.mark.asyncio
async def test_a_refusal_changes_nothing(reset, monkeypatch) -> None:
    pg = _Pg()
    qdrant = _wire(reset, monkeypatch, pg, [1, 2])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    with pytest.raises(SystemExit) as caught:
        await reset.reset_all(pg, assume_yes=False)

    assert caught.value.code == 2
    assert qdrant.deleted == [] and pg.executed == []


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_and_does_not_prompt(reset, monkeypatch) -> None:
    pg = _Pg()
    qdrant = _wire(reset, monkeypatch, pg, [1, 2])
    monkeypatch.setattr(reset, "DRY_RUN", True)

    await reset.reset_all(pg, assume_yes=False)

    assert qdrant.deleted == [] and pg.executed == []


@pytest.mark.asyncio
async def test_a_passage_left_with_an_embedding_id_is_an_error(reset, monkeypatch) -> None:
    pg = _Pg(left_after=3)
    _wire(reset, monkeypatch, pg, [1])
    with pytest.raises(SystemExit) as caught:
        await reset.reset_all(pg, assume_yes=True)
    assert caught.value.code == 1


@pytest.mark.asyncio
async def test_a_point_that_survives_the_delete_is_an_error(reset, monkeypatch) -> None:
    pg = _Pg()
    qdrant = _wire(reset, monkeypatch, pg, [1, 2, 3])

    async def _delete_nothing(**_kw: Any) -> None:
        return None

    monkeypatch.setattr(qdrant, "delete", _delete_nothing)
    with pytest.raises(RuntimeError, match="only 3 existed"):
        await reset.reset_all(pg, assume_yes=True)
    assert pg.executed == [], "Postgres must not be touched while old-space points remain"
