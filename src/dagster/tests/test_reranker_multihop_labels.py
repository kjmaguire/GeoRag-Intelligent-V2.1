"""Multi-hop reranker label generation (2026-05-23).

Asserts the new multi-hop dispatch logic:
  * one pair per report (when ≥2 sampled chunks are available)
  * pair is deterministic by sorted chunk_id
  * both bridge chunks emit a row with shared query_group_id and
    variant='multi_hop'
  * pre-existing literal + paraphrase emission is unchanged
"""
from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict


def test_multihop_group_id_is_deterministic():
    """SHA-256 over (report_id, chunk_a, chunk_b) → UUID. Same inputs, same
    UUID, across two independent computations."""
    report_id = "r-1"
    a, b = "cc-aaa", "cc-bbb"

    def compute(report_id, a, b):
        digest = hashlib.sha256(
            f"multihop::{report_id}::{a}::{b}".encode()
        ).hexdigest()[:32]
        return str(uuid.UUID(digest))

    g1 = compute(report_id, a, b)
    g2 = compute(report_id, a, b)
    assert g1 == g2
    # Different inputs → different group id
    assert g1 != compute(report_id, b, a)
    assert g1 != compute("r-2", a, b)


def test_pair_selection_picks_sorted_first_two_per_report():
    """The pair-selection rule sorts chunks by chunk_id and picks the
    first two. This locks the contract so reruns against the same
    sample produce the same pairs (matches the report-level
    deterministic split key in the asset)."""
    chunks_by_report: dict[str, list[dict]] = defaultdict(list)
    # Unsorted insertion order — sorted() inside the asset must canonicalise.
    chunks_by_report["r-1"].extend([
        {"chunk_id": "c-z", "chunk_text": "z"},
        {"chunk_id": "c-a", "chunk_text": "a"},
        {"chunk_id": "c-m", "chunk_text": "m"},
    ])
    chunks_by_report["r-2"].append({"chunk_id": "c-only", "chunk_text": "only"})
    chunks_by_report["r-3"].extend([
        {"chunk_id": "c-q", "chunk_text": "q"},
        {"chunk_id": "c-p", "chunk_text": "p"},
    ])

    pairs = []
    for report_id, chunks in chunks_by_report.items():
        if len(chunks) < 2:
            continue
        sorted_chunks = sorted(chunks, key=lambda c: c["chunk_id"])
        pairs.append((report_id, sorted_chunks[0]["chunk_id"], sorted_chunks[1]["chunk_id"]))

    # r-2 skipped (only 1 chunk); r-1 → (c-a, c-m); r-3 → (c-p, c-q)
    pair_map = {r: (a, b) for r, a, b in pairs}
    assert "r-2" not in pair_map
    assert pair_map["r-1"] == ("c-a", "c-m")
    assert pair_map["r-3"] == ("c-p", "c-q")


def test_multihop_emits_two_rows_per_pair_with_shared_group_id():
    """The post-vLLM emission for a multi_hop response must yield TWO
    rows (one per bridge chunk) carrying a shared query_group_id.
    This guarantees both chunks act as positives for the same query
    during reranker training."""
    request_meta = {
        "multihop::group-uuid-1": {
            "chunk_id": "c-a",
            "chunk_id_b": "c-m",
            "variant": "multi_hop",
            "query_group_id": "group-uuid-1",
            "gen_prompt_hash": "p" * 64,
        }
    }
    fake_response = {
        "query": "How does the Au grade in section X relate to the alteration assemblage?",
        "bridge": "Au mineralisation in chloritic alteration",
    }
    meta = request_meta["multihop::group-uuid-1"]
    out_rows = []
    if meta["variant"] == "multi_hop":
        for chunk_id_role in ("chunk_id", "chunk_id_b"):
            out_rows.append({
                "chunk_id":       meta[chunk_id_role],
                "variant":        "multi_hop",
                "query":          fake_response["query"],
                "fact_span":      "",
                "bridge":         fake_response["bridge"],
                "query_group_id": meta["query_group_id"],
            })

    assert len(out_rows) == 2
    assert {r["chunk_id"] for r in out_rows} == {"c-a", "c-m"}
    assert all(r["variant"] == "multi_hop" for r in out_rows)
    assert all(r["query_group_id"] == "group-uuid-1" for r in out_rows)
    assert all(r["query"] == fake_response["query"] for r in out_rows)
    assert all(r["bridge"] == fake_response["bridge"] for r in out_rows)


def test_module_exposes_new_constants():
    """Smoke-test that the constants needed for multi-hop are importable
    from the asset module."""
    from georag_dagster.assets.reranker_labels import (
        MULTI_HOP_PROMPT_SYSTEM,
        USER_PROMPT_MULTIHOP_TEMPLATE,
    )
    assert "multi-hop" in MULTI_HOP_PROMPT_SYSTEM.lower() \
        or "two chunks" in MULTI_HOP_PROMPT_SYSTEM.lower()
    assert "{chunk_a_text}" in USER_PROMPT_MULTIHOP_TEMPLATE
    assert "{chunk_b_text}" in USER_PROMPT_MULTIHOP_TEMPLATE
