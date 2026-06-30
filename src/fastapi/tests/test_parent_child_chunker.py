"""Unit tests for Plan §1b parent-child chunker.

Covers `pdf_ingester._chunk_pages` dispatch + `_group_into_parents`
behaviour. Companion to `docs/architecture/parent_child_chunker_spec.md`
§8 test plan.

DB-side wiring (the `_insert_passages` SQL change adding chunk_kind +
parent_chunk_id + COALESCE for passage_id_override) is exercised by
the existing integration tests for the ingest pipeline; this file
focuses on the pure-function chunker logic so it runs without a pool.
"""

from __future__ import annotations

import uuid

import pytest

from app.services.ingest.pdf_ingester import (
    _chunk_pages,
    _chunk_pages_flat,
    _group_into_parents,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _para(n: int) -> str:
    """Build a paragraph >= _MIN_CHUNK (100 chars) so the chunker keeps it."""
    return (
        f"Paragraph {n} text body discussing the geological context, "
        f"alteration, and mineralisation patterns observed in the drill "
        f"hole interval. " * 2
    )


def _page(n_paragraphs: int) -> str:
    """Build a page text with n_paragraphs paragraphs separated by blank lines."""
    return "\n\n".join(_para(i) for i in range(n_paragraphs))


def _is_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
        return True
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Flag-off / legacy path — byte-identical-ish to pre-§1b behaviour
# ---------------------------------------------------------------------------


def test_flag_off_returns_flat_narrative_chunks():
    """When parent_chunking=False, output is flat narrative only — no
    parents, no parent_chunk_id, no passage_id_override."""
    pages = [_page(5)]
    chunks = _chunk_pages(pages, parent_chunking=False)

    assert len(chunks) > 0
    for c in chunks:
        assert c["chunk_kind"] == "narrative"
        assert c["parent_chunk_id"] is None
        assert "passage_id_override" not in c


def test_flag_off_matches_chunk_pages_flat_directly():
    """The dispatcher with flag=False is exactly _chunk_pages_flat output."""
    pages = [_page(4), _page(3)]
    via_dispatcher = _chunk_pages(pages, parent_chunking=False)
    via_direct = _chunk_pages_flat(pages)
    assert via_dispatcher == via_direct


# ---------------------------------------------------------------------------
# Flag-on / parent-child path — happy paths
# ---------------------------------------------------------------------------


def test_six_children_at_n3_emit_two_parents_plus_six_children():
    """6 children with N=3 → 2 parents + 6 children = 8 rows total.

    Spec §8: "Flag on + 6 children → emits 2 parents + 6 children = 8 rows"
    """
    children = _chunk_pages_flat([_page(6)])
    # The page-fitter may collapse paragraphs into fewer chunks; force
    # exactly 6 by passing 6 explicit children to _group_into_parents.
    assert len(children) >= 1, "fixture didn't produce children"

    fake_6 = [
        {
            "text": f"child {i} text",
            "ordinal": i,
            "page_first": 1,
            "page_last": 1,
            "text_hash": f"h{i}",
            "chunk_kind": "narrative",
            "parent_chunk_id": None,
        }
        for i in range(6)
    ]
    out = _group_into_parents(fake_6, parents_per_group=3)
    assert len(out) == 8

    # First row = parent, then 3 children, then parent, then 3 children
    assert out[0]["chunk_kind"] == "section"
    assert all(out[i]["chunk_kind"] == "paragraph" for i in (1, 2, 3))
    assert out[4]["chunk_kind"] == "section"
    assert all(out[i]["chunk_kind"] == "paragraph" for i in (5, 6, 7))


def test_seven_children_at_n3_emit_two_parents_six_children_one_flat_tail():
    """7 children with N=3 → 2 parents + 6 children + 1 narrative tail = 9 rows.

    Spec §8: "Flag on + 7 children → 2 parents + 6 children + 1 flat tail = 9 rows"
    """
    fake_7 = [
        {
            "text": f"child {i} text",
            "ordinal": i,
            "page_first": 1,
            "page_last": 1,
            "text_hash": f"h{i}",
            "chunk_kind": "narrative",
            "parent_chunk_id": None,
        }
        for i in range(7)
    ]
    out = _group_into_parents(fake_7, parents_per_group=3)
    assert len(out) == 9

    # Last row is the tail singleton: flat narrative, no parent.
    tail = out[-1]
    assert tail["chunk_kind"] == "narrative"
    assert tail["parent_chunk_id"] is None
    assert "passage_id_override" not in tail


def test_single_child_emits_one_flat_row_no_parent():
    """Spec §8: "Flag on + 1 child → 1 flat row, no parent"

    Edge case: tiny doc. Never emit a 1-child parent that just duplicates.
    """
    fake_1 = [{
        "text": "lone child", "ordinal": 0,
        "page_first": 1, "page_last": 1,
        "text_hash": "h0", "chunk_kind": "narrative", "parent_chunk_id": None,
    }]
    out = _group_into_parents(fake_1, parents_per_group=3)
    assert len(out) == 1
    assert out[0]["chunk_kind"] == "narrative"
    assert out[0]["parent_chunk_id"] is None


def test_empty_input_returns_empty_output():
    """Edge case: no children → no output."""
    assert _group_into_parents([], parents_per_group=3) == []


# ---------------------------------------------------------------------------
# Reconstruction correctness — parent text + page span
# ---------------------------------------------------------------------------


def test_parent_text_is_double_newline_join_of_children():
    """Spec §8: "Parent text = concat of children with \\n\\n separator"

    Reconstruction correctness: a downstream consumer must be able to
    derive the parent text from the children.
    """
    fake_3 = [
        {"text": "alpha", "ordinal": i, "page_first": 1, "page_last": 1,
         "text_hash": f"h{i}", "chunk_kind": "narrative", "parent_chunk_id": None}
        for i in range(3)
    ]
    fake_3[0]["text"] = "first paragraph"
    fake_3[1]["text"] = "second paragraph"
    fake_3[2]["text"] = "third paragraph"

    out = _group_into_parents(fake_3, parents_per_group=3)
    parent = out[0]
    assert parent["text"] == "first paragraph\n\nsecond paragraph\n\nthird paragraph"


def test_parent_page_first_and_page_last_span_children():
    """Spec §8: parent page_first = first child's; page_last = last child's.

    Multi-page parents inherit the span correctly so the trace inspector
    can show "pp. 5-9" not "p. 5".
    """
    fake_3 = [
        {"text": f"text{i}", "ordinal": i, "page_first": p, "page_last": p,
         "text_hash": f"h{i}", "chunk_kind": "narrative", "parent_chunk_id": None}
        for i, p in zip(range(3), [5, 6, 9], strict=True)
    ]
    out = _group_into_parents(fake_3, parents_per_group=3)
    parent = out[0]
    assert parent["page_first"] == 5
    assert parent["page_last"] == 9


# ---------------------------------------------------------------------------
# UUID + FK wiring
# ---------------------------------------------------------------------------


def test_parent_has_passage_id_override_uuid_children_do_not():
    """Spec §3: parents pre-generate UUIDs in Python; children let SQL
    generate via gen_random_uuid(). So only parents carry the override."""
    fake_3 = [
        {"text": f"t{i}", "ordinal": i, "page_first": 1, "page_last": 1,
         "text_hash": f"h{i}", "chunk_kind": "narrative", "parent_chunk_id": None}
        for i in range(3)
    ]
    out = _group_into_parents(fake_3, parents_per_group=3)
    parent = out[0]
    assert "passage_id_override" in parent
    assert _is_uuid(parent["passage_id_override"])

    for child in out[1:4]:
        assert "passage_id_override" not in child


def test_children_parent_chunk_id_matches_parent_uuid():
    """Spec §8: "Children carry parent's UUID in parent_chunk_id"

    The FK relationship is established at chunk-time so _insert_passages
    can write it without a two-pass round-trip.
    """
    fake_3 = [
        {"text": f"t{i}", "ordinal": i, "page_first": 1, "page_last": 1,
         "text_hash": f"h{i}", "chunk_kind": "narrative", "parent_chunk_id": None}
        for i in range(3)
    ]
    out = _group_into_parents(fake_3, parents_per_group=3)
    parent_uuid = out[0]["passage_id_override"]
    for child in out[1:4]:
        assert child["parent_chunk_id"] == parent_uuid


def test_two_groups_get_distinct_parent_uuids():
    """Two parent groups must NOT share a UUID — each gets its own."""
    fake_6 = [
        {"text": f"t{i}", "ordinal": i, "page_first": 1, "page_last": 1,
         "text_hash": f"h{i}", "chunk_kind": "narrative", "parent_chunk_id": None}
        for i in range(6)
    ]
    out = _group_into_parents(fake_6, parents_per_group=3)
    parent_uuids = [out[0]["passage_id_override"], out[4]["passage_id_override"]]
    assert parent_uuids[0] != parent_uuids[1]
    # Children of group 1 point at parent 1; group 2 at parent 2.
    assert all(out[i]["parent_chunk_id"] == parent_uuids[0] for i in (1, 2, 3))
    assert all(out[i]["parent_chunk_id"] == parent_uuids[1] for i in (5, 6, 7))


# ---------------------------------------------------------------------------
# Ordinal numbering — parent BEFORE children in document order
# ---------------------------------------------------------------------------


def test_ordinals_are_renumbered_parent_before_children():
    """Parent ordinal must come BEFORE its children's ordinals so the
    INSERT order satisfies the FK constraint (parent row exists when
    child row references it).
    """
    fake_6 = [
        {"text": f"t{i}", "ordinal": 999, "page_first": 1, "page_last": 1,
         "text_hash": f"h{i}", "chunk_kind": "narrative", "parent_chunk_id": None}
        for i in range(6)
    ]
    out = _group_into_parents(fake_6, parents_per_group=3)
    # Ordinals should be 0, 1, 2, 3, 4, 5, 6, 7 contiguous from 0.
    assert [r["ordinal"] for r in out] == list(range(len(out)))
    # And the parent at index 0 has ordinal 0; its children get 1, 2, 3.
    assert out[0]["chunk_kind"] == "section"
    assert out[0]["ordinal"] == 0
    assert out[1]["ordinal"] == 1


# ---------------------------------------------------------------------------
# Discriminator values match parent_expansion._FETCH_PARENTS_SQL select
# ---------------------------------------------------------------------------


def test_chunk_kind_discriminator_values_match_parent_expansion_schema():
    """The parent_expansion module SELECTs chunk_kind from
    silver.document_passages. The values emitted here must match what
    the §3d expander expects so fetched parents have meaningful kinds.

    Test asserts the two new values are literally 'section' and
    'paragraph' (no enum drift).
    """
    fake_3 = [
        {"text": f"t{i}", "ordinal": i, "page_first": 1, "page_last": 1,
         "text_hash": f"h{i}", "chunk_kind": "narrative", "parent_chunk_id": None}
        for i in range(3)
    ]
    out = _group_into_parents(fake_3, parents_per_group=3)
    assert out[0]["chunk_kind"] == "section"
    for c in out[1:4]:
        assert c["chunk_kind"] == "paragraph"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_group_size_below_two_raises_value_error():
    """parents_per_group=1 makes no sense (1-child parents duplicate the
    child). The function rejects it explicitly to catch misconfiguration."""
    with pytest.raises(ValueError, match=r"parents_per_group must be"):
        _group_into_parents([{"text": "x", "ordinal": 0, "page_first": 1,
                              "page_last": 1, "text_hash": "h",
                              "chunk_kind": "narrative",
                              "parent_chunk_id": None}],
                            parents_per_group=1)


def test_group_size_n4_works():
    """Spec §10 open question: N=4 is the alternative group size. Verify
    it parses through cleanly so flipping the setting doesn't crash."""
    fake_8 = [
        {"text": f"t{i}", "ordinal": i, "page_first": 1, "page_last": 1,
         "text_hash": f"h{i}", "chunk_kind": "narrative", "parent_chunk_id": None}
        for i in range(8)
    ]
    out = _group_into_parents(fake_8, parents_per_group=4)
    # 2 parents + 8 children = 10 rows
    assert len(out) == 10
    assert out[0]["chunk_kind"] == "section"
    assert out[5]["chunk_kind"] == "section"


# ---------------------------------------------------------------------------
# Dispatcher — settings integration
# ---------------------------------------------------------------------------


def test_dispatcher_reads_settings_when_kwargs_omitted(monkeypatch):
    """When the caller passes no kwargs, _chunk_pages reads
    PARENT_CHUNKING_ENABLED from settings. Patch the setting to True
    and verify parent rows appear."""
    from app.config import settings  # noqa: PLC0415

    monkeypatch.setattr(settings, "PARENT_CHUNKING_ENABLED", True)
    monkeypatch.setattr(settings, "PARENT_CHUNKING_GROUP_SIZE", 3)

    # Build a page with enough paragraphs to force ≥ 3 chunks. Generous
    # buffer because the flat chunker may merge short paragraphs.
    pages = [_page(15)]
    out = _chunk_pages(pages)

    # Must contain at least one 'section' row when parent chunking is on.
    section_rows = [c for c in out if c.get("chunk_kind") == "section"]
    paragraph_rows = [c for c in out if c.get("chunk_kind") == "paragraph"]

    assert len(section_rows) >= 1, "expected at least one parent (section) row"
    assert len(paragraph_rows) >= 2, "expected children to outnumber parents"


def test_dispatcher_reads_settings_when_flag_off(monkeypatch):
    """Confirm the off-by-default behaviour: flag explicitly False →
    only narrative rows."""
    from app.config import settings  # noqa: PLC0415
    monkeypatch.setattr(settings, "PARENT_CHUNKING_ENABLED", False)

    pages = [_page(10)]
    out = _chunk_pages(pages)
    assert all(c["chunk_kind"] == "narrative" for c in out)
    assert all(c["parent_chunk_id"] is None for c in out)
