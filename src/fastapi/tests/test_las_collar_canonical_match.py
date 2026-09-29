"""A LAS well name that differs from the collar id only by separators or case
must attach to the existing collar, not create a phantom second one.

`_get_or_create_collar` matched `hole_id = $2` exactly. A collar file that
says `TR002` and a LAS whose ~WELL says `TR-002` (or `tr 002`) therefore did
not match; the ingester inserted a NEW collar whose coordinates come from
DEFAULT_UTM_FALLBACK — a fixed point in Wyoming — and hung the curves on it.
The real hole got no curves and the map got a hole 3,000 km from the project.
The tabular and well-log workflows already resolve ids through the canonical
form; this is the third writer brought into line.
"""

from __future__ import annotations

from app.services.ingest.las_ingester import _get_or_create_collar

_PJ = "b1000000-0000-0000-0000-0000000000a0"
_WS = "a0000000-0000-0000-0000-00000000feed"
_EXISTING = "d3000000-0000-0000-0000-0000000000c0"
_NEW = "e4000000-0000-0000-0000-0000000000d0"


class _Conn:
    """Mimics the two statements `_get_or_create_collar` issues."""

    def __init__(self, collars: list[tuple[str, str | None, str]]) -> None:
        self.collars = collars  # (hole_id, hole_id_canonical, collar_id)
        self.inserted: tuple | None = None

    async def fetchrow(self, sql: str, *args):
        flat = " ".join(sql.split())
        if flat.startswith("SELECT collar_id"):
            _project, hole_id, canonical = args
            exact = [c for c in self.collars if c[0] == hole_id]
            fuzzy = [
                c for c in self.collars
                if canonical is not None and c[1] == canonical
            ]
            hit = (exact or fuzzy or [None])[0]
            return {"collar_id": hit[2]} if hit else None
        if "INSERT INTO silver.collars" in flat:
            self.inserted = args
            return {"collar_id": _NEW}
        raise AssertionError(flat)


async def _resolve(conn: _Conn, well: str) -> str:
    return await _get_or_create_collar(
        conn, project_id=_PJ, hole_id=well, easting=480_000.0,
        northing=4_660_000.0, total_depth=61.5, drill_date=None,
        workspace_id=_WS,
    )


async def test_separator_and_case_variants_reuse_the_existing_collar() -> None:
    for well in ("TR-002", "tr 002", "TR_002", "TR002"):
        conn = _Conn([("TR002", "TR002", _EXISTING)])
        assert await _resolve(conn, well) == _EXISTING
        assert conn.inserted is None, well


async def test_exact_spelling_still_wins_over_a_canonical_twin() -> None:
    conn = _Conn([("TR-002", "TR002", "aaaa"), ("TR002", "TR002", "bbbb")])
    assert await _resolve(conn, "TR002") == "bbbb"


async def test_a_genuinely_new_hole_is_still_created() -> None:
    conn = _Conn([("TR001", "TR001", _EXISTING)])
    assert await _resolve(conn, "TR-002") == _NEW
    assert conn.inserted is not None
    assert conn.inserted[1] == "TR002"  # canonical stored on insert
