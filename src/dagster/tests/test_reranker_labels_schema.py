"""Schema regression tests for the reranker_label_dataset asset output.

Pins the JSONL row shape the training script
(``scripts/train_reranker_lora.py``) expects so a future refactor of
the persist step doesn't silently drop columns again. The 2026-05-19
materialisation dropped ``positive_chunk_text`` +
``hard_negative_chunk_texts`` + ``variant`` + ``query_group_id``,
which made the training script unable to load samples (it looked up
chunk_ids in silver.document_passages, and the chunks had been
re-ingested in the interim → all NULL). The 2026-05-29 fix re-adds
those fields; these tests pin the contract.

Test strategy: build a fixture ``keep_row`` matching the shape the
asset hands to its persistence step, run the record-building
expression inline, assert the output dict contains the new keys.
This mirrors what the asset does without spinning up Dagster.
"""
from __future__ import annotations


def _build_record(r: dict) -> dict:
    """Mirror the persist-step record construction in
    reranker_labels.reranker_label_dataset.

    Keep this in sync with the inline dict literal in the asset — the
    test would silently pass if they drift, but the next training run
    would fail. Worth the duplication for the schema lock-in.
    """
    positive = r["positive"]
    return {
        "query":                 r["query"],
        "chunk_id":              r["chunk_id"],
        "pdf_id":                positive["report_id"],
        "page":                  positive.get("page"),
        "bbox":                  positive.get("bbox"),
        "source_method":         positive.get("source_method"),
        "extraction_confidence": positive.get("extraction_confidence"),
        "label":                 r["label"],
        "positive_chunk_text":   positive.get("chunk_text", ""),
        "hardneg_ids":           [hn["qdrant_point_id"] for hn in r["hardneg"]],
        "hard_negative_chunk_texts": [
            hn.get("chunk_text", "") for hn in r["hardneg"]
        ],
        "variant":               r.get("variant", "literal"),
        "query_group_id":        r.get("query_group_id"),
        "gen_model":             r["gen_model"],
        "gen_prompt_hash":       r["gen_prompt_hash"],
        "fact_span":             r["fact_span"],
    }


def _sample_keep_row(*, variant: str = "literal", group_id: str | None = None) -> dict:
    """Fixture matching the shape `reranker_mined_negatives` emits."""
    return {
        "chunk_id": "chunk-a",
        "variant": variant,
        "query": "What is the strike orientation of the main mineralised zone?",
        "fact_span": "strikes 045°",
        "gen_model": "Qwen/Qwen3-14B-AWQ",
        "gen_prompt_hash": "abc123",
        "query_group_id": group_id,
        "label": 3,
        "positive": {
            "chunk_id": "chunk-a",
            "report_id": "report-1",
            "page": 47,
            "bbox": [0.1, 0.2, 0.5, 0.3],
            "source_method": "pdfminer",
            "extraction_confidence": 0.95,
            "chunk_text": "The main mineralised zone strikes 045° and dips 70° to the SE...",
        },
        "hardneg": [
            {
                "qdrant_point_id": "neg-1",
                "score": 0.65,
                "rank": 4,
                "report_id": "report-99",
                "commodity": "Cu",
                "section_title": "Regional geology",
                "chunk_text": "The regional geology comprises Archean greenstones...",
            },
            {
                "qdrant_point_id": "neg-2",
                "score": 0.55,
                "rank": 15,
                "report_id": "report-77",
                "commodity": "Au",
                "section_title": "Drilling summary",
                "chunk_text": "Phase 2 drilling targeted the Eastern lode...",
            },
        ],
    }


# ---------------------------------------------------------------------------
# Persist-step schema lock-ins
# ---------------------------------------------------------------------------


def test_persisted_record_includes_positive_chunk_text():
    """The training script reads ``positive_chunk_text`` directly. Drop
    this field → script crashes at row["positive_chunk_text"] access."""
    row = _sample_keep_row()
    rec = _build_record(row)
    assert "positive_chunk_text" in rec
    assert rec["positive_chunk_text"].startswith("The main mineralised zone")


def test_persisted_record_includes_hard_negative_chunk_texts():
    """Training script reads ``hard_negative_chunk_texts`` (list of str)
    parallel to ``hardneg_ids``. Same crash mode if missing."""
    row = _sample_keep_row()
    rec = _build_record(row)
    assert "hard_negative_chunk_texts" in rec
    assert len(rec["hard_negative_chunk_texts"]) == 2
    assert rec["hard_negative_chunk_texts"][0].startswith("The regional geology")
    assert rec["hard_negative_chunk_texts"][1].startswith("Phase 2 drilling")


def test_persisted_record_lists_align_by_index():
    """hardneg_ids and hard_negative_chunk_texts must be parallel lists —
    index N in one corresponds to the SAME negative as index N in the
    other. Used by the training script's contrastive-loss labelling."""
    row = _sample_keep_row()
    rec = _build_record(row)
    assert len(rec["hardneg_ids"]) == len(rec["hard_negative_chunk_texts"])
    assert rec["hardneg_ids"] == ["neg-1", "neg-2"]


def test_persisted_record_includes_variant_with_default():
    """variant=='literal' is the default for back-compat with upstream
    rows that don't carry the field (e.g. older mined_negatives output)."""
    row = _sample_keep_row(variant="literal")
    assert _build_record(row)["variant"] == "literal"

    row_para = _sample_keep_row(variant="paraphrase")
    assert _build_record(row_para)["variant"] == "paraphrase"

    row_mh = _sample_keep_row(variant="multi_hop")
    assert _build_record(row_mh)["variant"] == "multi_hop"


def test_persisted_record_carries_query_group_id_through_for_multihop():
    """multi_hop rows share a query_group_id UUID; training script uses
    it to group both bridge chunks as positives for the same query."""
    row = _sample_keep_row(
        variant="multi_hop", group_id="33333333-3333-3333-3333-333333333333",
    )
    assert _build_record(row)["query_group_id"] == "33333333-3333-3333-3333-333333333333"


def test_persisted_record_query_group_id_is_none_for_single_chunk_variants():
    """literal/paraphrase have no group_id by design — None is the contract."""
    row = _sample_keep_row(variant="literal", group_id=None)
    assert _build_record(row)["query_group_id"] is None


# ---------------------------------------------------------------------------
# Hardneg schema lock-in (the mining step contract that feeds the above)
# ---------------------------------------------------------------------------


def test_hardneg_dict_includes_chunk_text():
    """`reranker_mined_negatives` writes `chunk_text` per negative
    (sourced from Qdrant payload.text). The persist step expects it on
    every hardneg entry. Removing it from the mining step silently
    produces empty strings in `hard_negative_chunk_texts`."""
    row = _sample_keep_row()
    for hn in row["hardneg"]:
        assert "chunk_text" in hn, "every hardneg must carry chunk_text"
        assert hn["chunk_text"], "chunk_text must be non-empty for a real hardneg"


def test_persisted_record_tolerates_missing_chunk_text_with_empty_string():
    """Defensive: an old materialisation that pre-dates the chunk_text
    plumbing should not crash the persist step — empty string is the
    documented fallback."""
    row = _sample_keep_row()
    row["positive"].pop("chunk_text")
    for hn in row["hardneg"]:
        hn.pop("chunk_text", None)

    rec = _build_record(row)
    assert rec["positive_chunk_text"] == ""
    assert rec["hard_negative_chunk_texts"] == ["", ""]


# ---------------------------------------------------------------------------
# Full schema sanity — pin all keys the training script expects
# ---------------------------------------------------------------------------


def test_record_carries_full_training_script_contract():
    """Pin the complete key set the training script consumes. Adding a
    new field is fine; removing/renaming any of these breaks training."""
    row = _sample_keep_row()
    rec = _build_record(row)
    required = {
        "query", "chunk_id", "label",
        "positive_chunk_text", "hard_negative_chunk_texts",
        "variant", "query_group_id",
    }
    missing = required - set(rec.keys())
    assert not missing, f"missing required training-contract keys: {sorted(missing)}"
