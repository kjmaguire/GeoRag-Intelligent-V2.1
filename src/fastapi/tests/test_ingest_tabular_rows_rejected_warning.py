"""A partly-landed drill sheet must say how many rows the parser threw away.

The four CSV parsers validate row by row. A lithology interval with no
lithology code or an inverted from/to is rejected whole, and so is a survey
station with a dip the range check refuses. Those drops land in
`skipped_details`, which ingest_tabular read only when the writer got NOTHING
(`_refusal_reason`). A log that landed 2 of 5 rows finished with the same
headline as one that landed all 5 — the missing holes had no strip log and
nothing said why.

(An unrecognised OPTIONAL value - a free-text `Texture` - is no longer a
rejection at all: the row is kept with that field blank and the parse records
`optional_values_blanked`. See test_optional_values_blanked.py in
georag_geoparsers. These fixtures therefore reject on REQUIRED fields.)
"""

from __future__ import annotations

from pathlib import Path

from app.hatchet_workflows.ingest_tabular import _rows_rejected_warning

try:
    from georag_geoparsers import parse_csv_lithology
except ImportError:  # pragma: no cover - package always present in CI
    parse_csv_lithology = None

import pytest

_CSV = (
    "HoleID,From,To,Lithology,Texture\n"
    "TR001,0,5,Andesite,Fine\n"
    "TR001,5,9,Tuff,porphyritic\n"      # free-text texture: kept, texture blanked
    "TR001,9,12,Andesite,Coarse\n"
    "TR002,0,4,,massive\n"              # no lithology code: rejected
    "TR002,4,3,Andesite,Fine\n"         # inverted interval: rejected
)


@pytest.mark.skipif(parse_csv_lithology is None, reason="georag_geoparsers missing")
def test_partial_lithology_landing_reports_the_rejected_rows(tmp_path: Path) -> None:
    path = tmp_path / "lith.csv"
    path.write_text(_CSV)
    result = parse_csv_lithology(str(path))
    assert result.valid_rows == 3 and result.skipped_rows == 2

    note = _rows_rejected_warning(
        label="lith.csv", write_type="lithology", result=result, written=3,
    )

    assert note is not None
    assert note["code"] == "rows_rejected"
    assert "2 of 5 lithology row(s)" in note["message"]
    assert "missing_required x1" in note["detail"]
    assert "depth_order_invalid x1" in note["detail"]
    assert "re-upload" in note["detail"]
    # The kept-with-a-blank row is reported by ITS OWN warning, not this one.
    assert "invalid_categorical_value" not in note["detail"]
    assert any(w["code"] == "optional_values_blanked" for w in result.warnings)


@pytest.mark.skipif(parse_csv_lithology is None, reason="georag_geoparsers missing")
def test_clean_file_and_all_rejected_file_add_nothing(tmp_path: Path) -> None:
    path = tmp_path / "ok.csv"
    path.write_text("HoleID,From,To,Lithology\nTR001,0,5,Andesite\n")
    clean = parse_csv_lithology(str(path))
    assert _rows_rejected_warning(
        label="ok.csv", write_type="lithology", result=clean, written=1,
    ) is None

    # Nothing landed: the wrote_nothing warning owns that case.
    bad = tmp_path / "bad.csv"
    bad.write_text("HoleID,From,To,Lithology,Texture\nTR001,5,0,Tuff,Fine\n")
    rejected = parse_csv_lithology(str(bad))
    assert rejected.skipped_rows == 1
    assert _rows_rejected_warning(
        label="bad.csv", write_type="lithology", result=rejected, written=0,
    ) is None
