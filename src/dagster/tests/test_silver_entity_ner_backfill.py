"""Unit tests for silver_entity_ner_backfill — ADR-0007 PR-3.

Pure-function tests for the four entity extractors plus the re-anchor pass.
The full asset's SQL side is exercised via a MagicMock harness mirroring
``test_silver_raster_asset.py``.

Run with:
    pytest src/dagster/tests/test_silver_entity_ner_backfill.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from georag_dagster.assets.silver_entity_ner_backfill import (
    CONTRACTOR_ALLOWLIST,
    LAB_ALLOWLIST,
    SilverEntityNerBackfillConfig,
    extract_contractors,
    extract_hole_ids,
    extract_labs,
    extract_qps,
    majority,
    reanchor_candidates,
    silver_entity_ner_backfill,
)

# Direct-invocation handle — bypasses the @asset decorator's Dagster runtime
# wrapper so the test can pass MagicMock context / resources without the
# materialisation context machinery interfering.
_RAW_ASSET_FN = silver_entity_ner_backfill.op.compute_fn.decorated_fn


# ---------------------------------------------------------------------------
# extract_contractors
# ---------------------------------------------------------------------------

class TestExtractContractors:
    def test_allowlist_hit(self):
        text = "Drilling was undertaken by Major Drilling in 2024."
        out = extract_contractors(text)
        assert "Major Drilling" in out

    def test_allowlist_case_insensitive(self):
        text = "MAJOR drilling completed the program."
        # "major drilling" lowercased is in the allowlist
        out = extract_contractors(text)
        assert any("major drilling" in c.lower() for c in out)

    def test_by_verb_pattern_captures_unknown_org(self):
        text = "The 2023 drill program was undertaken by Acme Drillers Ltd."
        out = extract_contractors(text)
        assert any("Acme Drillers" in c for c in out)

    def test_verb_pattern_captures_unknown_org(self):
        text = "Acme Drillers performed the drilling between June and August 2023."
        out = extract_contractors(text)
        assert any("Acme Drillers" in c for c in out)

    def test_empty_text(self):
        assert extract_contractors("") == []
        assert extract_contractors(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# extract_qps
# ---------------------------------------------------------------------------

class TestExtractQps:
    def test_pgeo_credential(self):
        text = "John Smith, P.Geo, Senior Project Geologist signed the report."
        out = extract_qps(text)
        assert any("John Smith" in q for q in out)

    def test_qualified_person(self):
        text = "The Qualified Person for the report is Jane Doe, M.Sc., P.Eng."
        out = extract_qps(text)
        assert any("Jane Doe" in q for q in out)

    def test_logged_by_verb(self):
        text = "Hole PLS-22-08 was logged by Sam OConnor between July and August."
        out = extract_qps(text)
        assert any("Sam" in q for q in out)

    def test_dr_honorific_stripped(self):
        text = "supervised by Dr. Alice Brown during the 2022 program."
        out = extract_qps(text)
        assert any(q.startswith("Alice") for q in out)

    def test_single_word_rejected(self):
        # Need at least first + last to qualify as a name.
        text = "logged by Bob — that is too vague to count."
        out = extract_qps(text)
        assert "Bob" not in out


class TestExtractQpsStopwordFilter:
    """Stopword-filter tests for the QP extractor.

    The live ADR-0007 PR-3 first run saw 77 candidates / 8 landed: the
    credential-trailing regex captured sentence fragments like
    "estimates were prepared by" and "the Qualified Persons listed".
    These tests pin the tightened extraction so fragments are rejected
    BEFORE they reach the downstream `qp_name` IS NULL filter.
    """

    def test_verb_fragment_rejected(self):
        text = (
            "Resource estimates were prepared by the Qualified Persons "
            "listed in Section 2."
        )
        out = extract_qps(text)
        # No capture should contain a verb / header token.
        for q in out:
            toks = q.lower().split()
            assert "prepared" not in toks, f"verb 'prepared' leaked into {q!r}"
            assert "listed" not in toks, f"verb 'listed' leaked into {q!r}"
            assert "persons" not in toks, f"header 'Persons' leaked into {q!r}"
            assert "qualified" not in toks, f"header 'Qualified' leaked into {q!r}"

    def test_pronoun_and_header_fragment_rejected(self):
        # "The Authors" looks like a 2-token capitalised name but is a
        # report-section header.
        text = "The Authors, P.Geo, prepared and signed the report."
        out = extract_qps(text)
        for q in out:
            toks = q.lower().split()
            assert "the" not in toks, f"pronoun 'the' leaked into {q!r}"
            assert "authors" not in toks, f"header 'Authors' leaked into {q!r}"

    def test_real_name_survives_alongside_noise(self):
        # Mixed: noise next to a real QP. The noise must be filtered AND
        # the real name must still be extracted.
        text = (
            "Resource estimates were prepared by the Qualified Persons "
            "listed in Section 2, including John Smith, P.Geo, Senior "
            "Project Geologist, who supervised the program."
        )
        out = extract_qps(text)
        assert any("John Smith" in q for q in out), (
            f"expected 'John Smith' in extracted names, got {out!r}"
        )
        for q in out:
            toks = q.lower().split()
            assert not any(t in {"listed", "prepared", "persons", "qualified", "the"}
                           for t in toks), f"stopword token leaked into {q!r}"

    def test_estimated_verb_rejected(self):
        text = "Mineral resources were estimated by John Doe, P.Geo."
        out = extract_qps(text)
        assert any("John Doe" in q for q in out)
        for q in out:
            assert "estimated" not in q.lower().split()

    def test_leading_header_trimmed_to_real_name(self):
        # The greedy regex sometimes pulls a leading "Qualified Persons"
        # header in front of a real name. `_strip_stopword_edges` should
        # peel it off so the credential still binds to the actual person.
        text = "Qualified Persons John Smith, P.Geo, supervised the work."
        out = extract_qps(text)
        assert any("John Smith" in q for q in out), (
            f"expected leading header to be trimmed; got {out!r}"
        )


# ---------------------------------------------------------------------------
# extract_labs
# ---------------------------------------------------------------------------

class TestExtractLabs:
    def test_als_allowlist(self):
        text = "Assays were analyzed at ALS Geochemistry in Vancouver."
        out = extract_labs(text)
        assert any("ALS" in lab for lab in out)

    def test_verb_pattern_unknown_lab(self):
        text = "Samples were assayed at Northern Analytics Inc."
        out = extract_labs(text)
        assert any("Northern Analytics" in lab for lab in out)

    def test_sgs(self):
        text = "Pulps were submitted to SGS Canada for fire-assay finishing."
        out = extract_labs(text)
        assert any("SGS" in lab for lab in out)


# ---------------------------------------------------------------------------
# extract_hole_ids
# ---------------------------------------------------------------------------

class TestExtractHoleIds:
    def test_lettered_hole_id(self):
        text = "Hole PLS-22-08 intersected mineralisation at 245.5m."
        ids = [h for h, _s, _e in extract_hole_ids(text)]
        assert "PLS-22-08" in ids

    def test_numeric_hole_id_requires_context(self):
        text = "Drillhole 36-1085 returned strong assays."
        ids = [h for h, _s, _e in extract_hole_ids(text)]
        assert "36-1085" in ids

    def test_numeric_without_context_skipped(self):
        text = "Interval 36-1085 metres in the upper section."
        ids = [h for h, _s, _e in extract_hole_ids(text)]
        # No "hole/drillhole/ddh" precedes 36-1085 → not a hole id
        assert "36-1085" not in ids

    def test_positions_returned(self):
        text = "Hole PLS-22-08 logged data."
        hits = extract_hole_ids(text)
        assert len(hits) == 1
        hid, start, end = hits[0]
        assert hid == "PLS-22-08"
        assert text[start:end].upper() == "PLS-22-08"


# ---------------------------------------------------------------------------
# majority helper
# ---------------------------------------------------------------------------

class TestMajority:
    def test_picks_most_frequent(self):
        assert majority(["a", "b", "a", "c", "a"]) == "a"

    def test_ignores_none_and_empty(self):
        assert majority([None, "", "x"]) == "x"

    def test_empty_returns_none(self):
        assert majority([]) is None
        assert majority([None, ""]) is None


# ---------------------------------------------------------------------------
# reanchor_candidates — the core PR-2 re-binding logic
# ---------------------------------------------------------------------------

class TestReanchorCandidates:
    def test_foliation_near_hole_id_reanchored(self):
        text = (
            "Foliation 045/72 SE was logged in hole CAM-12-001 at 245.5m — "
            "a planar fabric consistent with the regional trend."
        )
        lookup = {"CAM-12-001": "11111111-1111-1111-1111-111111111111"}
        out = reanchor_candidates(text=text, hole_id_to_collar=lookup)
        assert len(out) == 1
        row = out[0]
        assert row["collar_id"] == "11111111-1111-1111-1111-111111111111"
        assert row["structure_type"] == "foliation"
        assert row["true_dip"] == 72.0

    def test_no_hole_nearby_drops_row(self):
        # Structural notation present but no hole within ±300 chars.
        text = "Foliation 045/72 SE was observed in outcrop along the road."
        lookup = {"CAM-12-001": "11111111-1111-1111-1111-111111111111"}
        out = reanchor_candidates(text=text, hole_id_to_collar=lookup)
        assert out == []

    def test_unknown_hole_id_drops_row(self):
        text = "Foliation 045/72 SE was logged in hole CAM-12-001 at 245m."
        # CAM-12-001 not in lookup → no match.
        out = reanchor_candidates(text=text, hole_id_to_collar={})
        assert out == []

    def test_closest_hole_wins(self):
        text = (
            "Hole AAA-00-01 was completed in 2020. "
            "Much later, joint set 080/55 NE was logged in hole BBB-00-02 at 100m."
        )
        lookup = {
            "AAA-00-01": "aaa-collar",
            "BBB-00-02": "bbb-collar",
        }
        out = reanchor_candidates(text=text, hole_id_to_collar=lookup)
        assert len(out) == 1
        assert out[0]["collar_id"] == "bbb-collar"
        assert out[0]["structure_type"] == "joint"


# ---------------------------------------------------------------------------
# Allowlist sanity
# ---------------------------------------------------------------------------

class TestAllowlistsAreLowercaseDistinct:
    def test_contractor_allowlist_has_no_duplicates(self):
        norm = [c.lower() for c in CONTRACTOR_ALLOWLIST]
        assert len(set(norm)) == len(norm)

    def test_lab_allowlist_has_no_duplicates(self):
        norm = [c.lower() for c in LAB_ALLOWLIST]
        assert len(set(norm)) == len(norm)


# ---------------------------------------------------------------------------
# Full-asset smoke — mocked Postgres + mocked Neo4j
# ---------------------------------------------------------------------------

def _build_mock_postgres(report_rows: list[dict], collar_rows: list[dict],
                         existing_structure_rows: list[dict] | None = None):
    """Construct a MagicMock PostgresResource that returns the given rows.

    The asset issues three groups of queries (reports + per-project collar
    lookups + structure dedupe). Each fetchall() returns the next item from
    a queued list so the test controls exactly what each cursor sees.
    """
    existing_structure_rows = existing_structure_rows or []
    fetch_queue: list[list[dict]] = [report_rows]
    # One per project_id seen in report_rows (UNORDERED via set; we line them
    # up dynamically below by stashing all collar rows under a marker and
    # returning the same list each time — the mock cursor doesn't know which
    # SQL was executed). For the simple test fixtures we use one project.
    fetch_queue.append(collar_rows)
    fetch_queue.append(existing_structure_rows)

    cursor = MagicMock()
    cursor.__enter__ = lambda s: s
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchall.side_effect = fetch_queue
    cursor.rowcount = 1

    conn = MagicMock()
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor
    conn.commit = MagicMock()

    postgres = MagicMock()
    postgres.get_connection.return_value = conn
    return postgres, cursor


def _build_mock_neo4j():
    neo4j = MagicMock()
    session = MagicMock()
    session.__enter__ = lambda s: s
    session.__exit__ = MagicMock(return_value=False)
    driver = MagicMock()
    driver.session.return_value = session
    neo4j.get_driver.return_value = driver
    return neo4j, session, driver


class TestSilverEntityNerBackfillAsset:
    @patch("georag_dagster.assets.silver_entity_ner_backfill.psycopg2.extras.execute_batch")
    def test_end_to_end_smoke(self, mock_execute_batch):
        """End-to-end mocked smoke covering all four entity classes + re-anchor."""
        workspace_id = "a0000000-0000-0000-0000-000000000001"
        project_id = "b0000000-0000-0000-0000-000000000002"
        text = (
            "Drilling was undertaken by Major Drilling in 2024. "
            "John Smith, P.Geo, Senior Project Geologist supervised the program. "
            "Assays were analyzed at ALS Geochemistry. "
            "Hole PLS-22-08 was logged by Jane Doe with foliation 045/72 SE at 245.5m."
        )
        report_rows = [{
            "report_id": "r0000000-0000-0000-0000-000000000001",
            "project_id": project_id,
            "sections_text": text,
            "authors": ["John Smith"],
            "qp_name": [],
        }]
        collar_rows = [{
            "collar_id": "c0000000-0000-0000-0000-000000000010",
            "hole_id_canonical": "PLS-22-08",
        }]
        postgres, cursor = _build_mock_postgres(report_rows, collar_rows)
        neo4j, session, _driver = _build_mock_neo4j()

        config = SilverEntityNerBackfillConfig(
            workspace_id=workspace_id, project_id=project_id,
        )
        context = MagicMock()

        result = _RAW_ASSET_FN(
            context=context, config=config, postgres=postgres, neo4j=neo4j,
        )

        # The asset surfaces NER counts + UPDATE counts + reanchor count.
        md = result.metadata
        assert md["reports_scanned"].value == 1
        # Contractor allowlist + verb pattern → at least 1
        assert md["contractors_found"].value >= 1
        # John Smith (titled) + Jane Doe (logged by) + author John Smith
        assert md["geologists_found"].value >= 2
        # ALS Geochemistry from the allowlist
        assert md["labs_found"].value >= 1
        # PLS-22-08
        assert md["hole_ids_found"].value >= 1
        # Re-anchor: foliation 045/72 within 300 chars of "Hole PLS-22-08"
        assert md["structure_reanchored"].value >= 1
        # Neo4j MERGE fired for at least one QP
        assert md["qp_nodes_merged"].value >= 1
        session.run.assert_called()

    @patch("georag_dagster.assets.silver_entity_ner_backfill.psycopg2.extras.execute_batch")
    def test_idempotency_via_where_is_null(self, mock_execute_batch):
        """Asset uses WHERE col IS NULL, so a 2nd run on already-populated DB
        produces 0 UPDATEs. We model that by returning rowcount=0."""
        workspace_id = "a0000000-0000-0000-0000-000000000001"
        project_id = "b0000000-0000-0000-0000-000000000002"
        report_rows = [{
            "report_id": "r0000000-0000-0000-0000-000000000001",
            "project_id": project_id,
            "sections_text": "Drilling by Major Drilling.",
            "authors": [],
            "qp_name": ["John Smith"],
        }]
        postgres, cursor = _build_mock_postgres(report_rows, [])
        cursor.rowcount = 0  # All UPDATEs no-op on the 2nd run.
        neo4j, _session, _driver = _build_mock_neo4j()

        config = SilverEntityNerBackfillConfig(
            workspace_id=workspace_id, project_id=project_id,
        )
        context = MagicMock()

        result = _RAW_ASSET_FN(
            context=context, config=config, postgres=postgres, neo4j=neo4j,
        )

        md = result.metadata
        # NER side still finds entities (it's reading not writing).
        assert md["contractors_found"].value >= 1
        # But UPDATEs landed 0 rows because rowcount=0 → idempotent.
        assert md["campaigns_updated"].value == 0
        assert md["assays_updated"].value == 0
