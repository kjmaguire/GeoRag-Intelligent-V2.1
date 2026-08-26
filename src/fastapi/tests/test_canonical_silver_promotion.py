"""The §04e-canonical tables must have LIVE writers, and correct ones.

WHY THIS FILE EXISTS
    ``silver.assays_v2`` and ``silver.lithology`` are the canonical pair
    (Kyle 2026-05-20, reaffirmed 2026-08-25). Every text-retrieval and UI
    reader points at them: nl_summaries' synthesizers, the agent's assay
    tools, AssayResolver citations, the DrillholeDetail assay panel and
    quality badge, CsvLithologyExporter. Their only writers were Dagster
    assets, retired 2026-07-28 — from that day both tables were permanently
    empty (measured 0 rows on live Azure 2026-08-25 beside 827 geochemistry
    rows) and every one of those readers returned nothing.

    Worse: the replacement Hatchet ingest DROPPED per-element assay values
    entirely. ``csv_sample.py`` extracts ``commodity_assays`` per record;
    the old ``_SAMPLE_SQL`` had no such column, so the values reached no
    table at all, in any form.

    Both writers live again as of 2026-08-25: ``ingest_tabular`` explodes
    sample records into ``silver.assays_v2`` in the same transaction as the
    samples write, and ``promote_silver_to_gold`` derives
    ``silver.lithology`` from ``silver.lithology_logs`` with the retired
    asset's rock-code resolution. Per the 2026-07-28 lesson, the wiring is
    asserted structurally — pure-function suites passed the whole time the
    tables sat empty.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
WORKFLOWS = APP / "hatchet_workflows"


def _function_node(tree: ast.Module, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return node
    raise AssertionError(f"no function named {name}")


def _names_loaded(node: ast.AST) -> set[str]:
    return {
        n.id for n in ast.walk(node)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }


def _string_constants(node: ast.AST) -> str:
    return "\n".join(
        n.value for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )


# ---------------------------------------------------------------------------
# Reachability — the half that matters
# ---------------------------------------------------------------------------
class TestItIsActuallyReachable:
    def test_sample_writer_persists_commodity_assays(self):
        """The parser's per-element dict must survive into silver.samples.

        Asserted on the SQL constant, not the file text, so a comment
        mentioning the column cannot satisfy it.
        """
        from app.hatchet_workflows.ingest_tabular import _SAMPLE_SQL

        assert "commodity_assays" in _SAMPLE_SQL
        assert "commodity_assay_flags" in _SAMPLE_SQL

    def test_assays_v2_write_is_wired_into_the_interval_writer(self):
        """_write_intervals must derive AND execute the assays_v2 rows."""
        source = (WORKFLOWS / "ingest_tabular.py").read_text(encoding="utf-8")
        writer = _function_node(ast.parse(source), "_write_intervals")
        names = _names_loaded(writer)
        assert "derive_assay_v2_rows" in names
        assert "_ASSAYS_V2_SQL" in names

    def test_assays_v2_replaces_before_inserting(self):
        """A corrected sample file must not double its holes' element rows."""
        source = (WORKFLOWS / "ingest_tabular.py").read_text(encoding="utf-8")
        writer = _function_node(ast.parse(source), "_write_intervals")
        assert "DELETE FROM silver.assays_v2" in _string_constants(writer)

    def test_lithology_canonical_promotion_is_wired_into_promote(self):
        """The promote task must call the silver→silver step."""
        source = (WORKFLOWS / "promote_silver_to_gold.py").read_text(
            encoding="utf-8",
        )
        promote = _function_node(ast.parse(source), "promote")
        assert "_promote_lithology_canonical" in _names_loaded(promote)

    def test_gold_lithology_bands_derive_from_the_canonical_table(self):
        """gold reads silver.lithology, not the legacy table it mirrors.

        Leaving the gold read on lithology_logs would fork the strip log
        from every other reader the moment rock-code resolution rewrites
        a code.
        """
        from app.hatchet_workflows.promote_silver_to_gold import (
            _INTERVALS_LITHOLOGY,
        )

        assert "FROM silver.lithology l" in _INTERVALS_LITHOLOGY
        assert "silver.lithology_logs" not in _INTERVALS_LITHOLOGY

    def test_canonical_step_runs_before_the_gold_reads(self):
        """The gold SQL in the same iteration must see the freshly
        promoted rows, so the silver→silver step goes first."""
        source = (WORKFLOWS / "promote_silver_to_gold.py").read_text(
            encoding="utf-8",
        )
        promote = _function_node(ast.parse(source), "promote")
        body = ast.get_source_segment(source, promote)
        assert body.index("_promote_lithology_canonical") < body.index(
            "_INTERVALS_LITHOLOGY",
        )

    def test_every_nl_summaries_source_table_has_a_live_writer(self):
        """The regression this whole change closes.

        nl_summaries reads three tables. Each must appear as an INSERT
        target in live (non-Dagster) code, or the synthesizers run
        cleanly and write nothing — for every project, forever.
        """
        from app.hatchet_workflows.ingest_tabular import (
            _ASSAYS_V2_SQL,
            _COLLAR_SQL,
        )
        from app.hatchet_workflows.nl_summaries import SYNTHESIZERS
        from app.hatchet_workflows.promote_silver_to_gold import (
            _LITHOLOGY_CANONICAL_INSERT,
        )

        writers = {
            "silver.assays_v2": _ASSAYS_V2_SQL,
            "silver.lithology": _LITHOLOGY_CANONICAL_INSERT,
            "silver.collars": _COLLAR_SQL,
        }
        for source_table, _sql, _renderer, _id in SYNTHESIZERS.values():
            assert source_table in writers, (
                f"nl_summaries reads {source_table} but no live writer "
                "is registered in this test — if the synthesizer set grew, "
                "wire the new table's writer in here too"
            )
            assert f"INSERT INTO {source_table}" in writers[source_table]


# ---------------------------------------------------------------------------
# derive_assay_v2_rows — pure arithmetic
# ---------------------------------------------------------------------------
def _rec(**overrides):
    base = {
        "sample_id": "S-1001",
        "hole_id": "EL-001",
        "from_depth": 10.0,
        "to_depth": 11.5,
        "lab_id": "ALS",
        "commodity_assays": {"Au_ppb": 1000.0},
        "commodity_assay_flags": None,
    }
    base.update(overrides)
    return base


def _derive(rec, element_ref=None):
    from app.hatchet_workflows.ingest_tabular import derive_assay_v2_rows

    return derive_assay_v2_rows(
        rec,
        workspace_id="a0000000-0000-0000-0000-000000000001",
        collar_id="b0000000-0000-0000-0000-000000000002",
        element_ref=element_ref or {},
    )


class TestDeriveAssayV2Rows:
    def test_tuple_arity_matches_the_sql(self):
        """A drifted placeholder count fails HERE, not mid-ingest."""
        from app.hatchet_workflows.ingest_tabular import _ASSAYS_V2_SQL

        placeholders = max(
            int(m) for m in re.findall(r"\$(\d+)", _ASSAYS_V2_SQL)
        )
        rows, _ = _derive(_rec())
        assert len(rows[0]) == placeholders

    def test_ppb_converts_to_ppm(self):
        rows, skipped = _derive(_rec(commodity_assays={"Au_ppb": 1000.0}))
        assert skipped == 0
        (_, _, _, sample_id, f, t, element, value, unit, ppm, *_rest) = rows[0]
        assert (sample_id, f, t) == ("S-1001", 10.0, 11.5)
        assert (element, value, unit) == ("Au", 1000.0, "ppb")
        assert ppm == pytest.approx(1.0)

    def test_pct_converts_to_ppm(self):
        rows, _ = _derive(_rec(commodity_assays={"Cu_pct": 1.5}))
        assert rows[0][6:10] == ("Cu", 1.5, "pct", pytest.approx(15000.0))

    def test_lab_casing_is_canonicalised(self):
        """The lab writes AU_PPM; the agent's tools match 'Au' verbatim."""
        rows, _ = _derive(_rec(commodity_assays={"AU_PPM": 2.0}))
        assert rows[0][6] == "Au"

    def test_u3o8_stays_u3o8(self):
        """Compound oxide grades are NOT folded into the element — 1 ppm
        U3O8 and 1 ppm U differ by a factor of 1.18 and a geologist knows
        which one the certificate reported."""
        rows, _ = _derive(_rec(commodity_assays={"U3O8_ppm": 250.0}))
        assert rows[0][6:10] == ("U3O8", 250.0, "ppm", pytest.approx(250.0))

    def test_suffixless_column_uses_element_reference_default(self):
        rows, _ = _derive(
            _rec(commodity_assays={"Cu": 1.5}), element_ref={"Cu": "pct"},
        )
        assert rows[0][8:10] == ("pct", pytest.approx(15000.0))

    def test_suffixless_unknown_element_does_not_guess_a_unit(self):
        rows, _ = _derive(_rec(commodity_assays={"Ti": 4.2}))
        (_, _, _, _, _, _, element, value, unit, ppm, *_rest) = rows[0]
        assert (element, value, unit) == ("Ti", 4.2, "unspecified")
        assert ppm is None

    def test_half_detection_limit_substitution_is_flagged(self):
        rows, _ = _derive(_rec(
            commodity_assays={"Au_ppb": 0.25},
            commodity_assay_flags={"Au_ppb": {
                "dl_flag": True, "dl_threshold": 0.5,
                "original": "<0.5", "substitution": "half_dl",
            }},
        ))
        (*_head, detection_limit, over, under, half_dl, _lab) = rows[0]
        assert (detection_limit, over, under, half_dl) == (0.5, False, True, True)

    def test_bdl_with_unknown_threshold_still_lands_as_a_row(self):
        """value NULL + under_detection is 'below detection', which is not
        the same statement as 'never analysed'."""
        rows, skipped = _derive(_rec(
            commodity_assays={},
            commodity_assay_flags={"Cu_pct": {
                "dl_flag": True, "dl_threshold": None,
                "original": "BDL", "substitution": "null",
            }},
        ))
        assert skipped == 0
        (_, _, _, _, _, _, element, value, _unit, ppm, dl, _o, under, *_r) = rows[0]
        assert (element, value, ppm, dl, under) == ("Cu", None, None, None, True)

    def test_unparseable_cells_are_not_measurements(self):
        rows, skipped = _derive(_rec(
            commodity_assays={},
            commodity_assay_flags={"Zn_ppm": {
                "unparseable": True, "original": "NS",
            }},
        ))
        assert rows == [] and skipped == 0

    def test_missing_sample_id_skips_and_counts(self):
        """assays_v2.sample_id is NOT NULL; a synthesised one would break
        the same-file-same-ids property. Skipped, visibly."""
        rows, skipped = _derive(_rec(
            sample_id=None,
            commodity_assays={"Au_ppb": 1.0, "Cu_pct": 2.0},
        ))
        assert rows == [] and skipped == 2

    def test_inverted_interval_skips_and_counts(self):
        rows, skipped = _derive(_rec(from_depth=12.0, to_depth=10.0))
        assert rows == [] and skipped == 1

    def test_negative_value_skips_that_element_only(self):
        """The table CHECK rejects negatives; one lab placeholder must not
        sink the row's other elements."""
        rows, skipped = _derive(_rec(
            commodity_assays={"Au_ppb": -1.0, "Cu_pct": 2.0},
        ))
        assert skipped == 1
        assert [r[6] for r in rows] == ["Cu"]

    def test_same_input_same_ids(self):
        """Re-uploading the same file must rewrite the same rows, or every
        re-ingest re-keys the nl_summaries passages derived from them."""
        first, _ = _derive(_rec())
        second, _ = _derive(_rec())
        assert first[0][0] == second[0][0]

    def test_each_element_gets_its_own_id(self):
        rows, _ = _derive(_rec(
            commodity_assays={"Au_ppb": 1.0, "Cu_pct": 2.0},
        ))
        assert len({r[0] for r in rows}) == 2


# ---------------------------------------------------------------------------
# Rock-code resolution — ported from the retired bronze_to_silver asset
# ---------------------------------------------------------------------------
def _lookup(rows=None):
    from app.hatchet_workflows.promote_silver_to_gold import (
        build_rock_code_lookup,
    )

    return build_rock_code_lookup(rows if rows is not None else [
        {"code": "SST", "name": "Sandstone", "system": "NRCAN"},
        {"code": "PGN", "name": "Paragneiss", "system": "GSC"},
        {"code": "GRN", "name": "Granite", "system": "NRCAN"},
    ])


class TestRockCodeResolution:
    def _resolve(self, raw, lookup=None):
        from app.hatchet_workflows.promote_silver_to_gold import (
            resolve_rock_code,
        )

        return resolve_rock_code(raw, lookup if lookup is not None else _lookup())

    def test_exact_code_match_any_case(self):
        assert self._resolve("sst") == ("SST", 1.0, "Sandstone")

    def test_exact_name_match(self):
        assert self._resolve("granite") == ("GRN", 1.0, "Granite")

    def test_fuzzy_match_carries_its_score_as_confidence(self):
        code, confidence, name = self._resolve("granitic")
        assert (code, name) == ("GRN", "Granite")
        assert confidence is not None and 0.6 <= confidence < 1.0

    def test_no_match_keeps_the_geologists_word(self):
        assert self._resolve("kimberlite") == (None, None, "kimberlite")

    def test_empty_and_none_resolve_to_nothing(self):
        assert self._resolve(None) == (None, None, None)
        assert self._resolve("   ") == (None, None, None)

    def test_preferred_system_wins_a_code_collision(self):
        lookup = _lookup([
            {"code": "GN", "name": "Greenstone", "system": "GSC"},
            {"code": "GN", "name": "Gneiss", "system": "NRCAN"},
        ])
        assert self._resolve("gn", lookup) == ("GN", 1.0, "Gneiss")

    def test_an_empty_catalogue_never_raises(self):
        assert self._resolve("granite", _lookup([])) == (
            None, None, "granite",
        )
