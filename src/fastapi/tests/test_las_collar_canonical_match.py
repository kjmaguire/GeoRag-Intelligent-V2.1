"""A LAS well name that differs from the collar id only by separators or case
must attach to the existing collar, not create a phantom second one.

The ingester used to match `hole_id = $2` exactly. A collar file that says
`TR002` and a LAS whose ~WELL says `TR-002` (or `tr 002`) therefore did not
match; the ingester inserted a NEW collar at a fabricated location and hung
the curves on it. The real hole got no curves and the map got a hole
thousands of kilometres from the project. The tabular and well-log workflows
already resolve ids through the canonical form; `_find_collar` brings the LAS
ingester into line. (Collar creation itself — only from declared
coordinates, never a fabricated location — is covered by
test_las_collar_location.py.)
"""

from __future__ import annotations

from app.services.ingest.las_ingester import _find_collar

_PJ = "b1000000-0000-0000-0000-0000000000a0"
_EXISTING = "d3000000-0000-0000-0000-0000000000c0"


class _Conn:
    """Mimics the exact-then-canonical lookups `_find_collar` issues."""

    def __init__(self, collars: list[tuple[str, str | None, str]]) -> None:
        self.collars = collars  # (hole_id, hole_id_canonical, collar_id), creation order

    async def fetchrow(self, sql: str, *args):
        flat = " ".join(sql.split())
        assert flat.startswith("SELECT collar_id"), flat
        _project, value = args
        if "hole_id_canonical = $2" in flat:
            hits = [c for c in self.collars if c[1] == value]
        else:
            hits = [c for c in self.collars if c[0] == value]
        return {"collar_id": hits[0][2]} if hits else None


async def test_separator_and_case_variants_reuse_the_existing_collar() -> None:
    for well in ("TR-002", "tr 002", "TR_002", "TR002"):
        conn = _Conn([("TR002", "TR002", _EXISTING)])
        assert await _find_collar(conn, project_id=_PJ, hole_id=well) == _EXISTING, well


async def test_exact_spelling_still_wins_over_a_canonical_twin() -> None:
    conn = _Conn([("TR-002", "TR002", "aaaa"), ("TR002", "TR002", "bbbb")])
    assert await _find_collar(conn, project_id=_PJ, hole_id="TR002") == "bbbb"


async def test_a_genuinely_new_hole_finds_nothing() -> None:
    conn = _Conn([("TR001", "TR001", _EXISTING)])
    assert await _find_collar(conn, project_id=_PJ, hole_id="TR-002") is None
