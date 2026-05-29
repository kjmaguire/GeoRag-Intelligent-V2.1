"""CC-04 — Tests for the auto-classifier."""

from __future__ import annotations

from app.services.domain_classifier import (
    DOMAIN_GEOCHEMISTRY,
    DOMAIN_GEOLOGY,
    DOMAIN_GEOPHYSICS,
    DOMAIN_REPORTS,
    classify_document,
)


def test_ni_43_101_filename_classifies_as_reports() -> None:
    out = classify_document("BattleNorth_NI_43-101_TechReport.pdf")
    assert any(a.domain_id == DOMAIN_REPORTS for a in out)
    reports = [a for a in out if a.domain_id == DOMAIN_REPORTS][0]
    assert reports.sub_type_id == 106  # NI 43-101 Technical Report
    assert reports.confidence >= 0.5
    assert "filename:ni43-101" in reports.matched_patterns


def test_assay_filename_classifies_as_geochemistry() -> None:
    out = classify_document("PLS-22-08_assay_certificate.pdf")
    domain_ids = {a.domain_id for a in out}
    assert DOMAIN_GEOCHEMISTRY in domain_ids


def test_lithology_log_filename_classifies_as_geology() -> None:
    out = classify_document("PLS-22-08_lithology_log.csv")
    domain_ids = {a.domain_id for a in out}
    assert DOMAIN_GEOLOGY in domain_ids
    geo = [a for a in out if a.domain_id == DOMAIN_GEOLOGY][0]
    assert geo.sub_type_id == 208  # logged_lithology


def test_airborne_mag_classifies_as_geophysics() -> None:
    out = classify_document("Springpole_Airborne_Magnetic_Survey_2024.pdf")
    domain_ids = {a.domain_id for a in out}
    assert DOMAIN_GEOPHYSICS in domain_ids
    geophys = [a for a in out if a.domain_id == DOMAIN_GEOPHYSICS][0]
    assert geophys.sub_type_id == 408  # airborne_mag


def test_multi_domain_drill_program_returns_three_tags() -> None:
    """A drill program filename can legitimately span Geology +
    Geochemistry + Geophysics — multi-domain is the explicit MVP rule."""
    out = classify_document(
        "PLS-22-08_drill_program_lithology_assay_downhole_em.zip",
        content_snippet="core log entries: foliated granite, sulfide veining...",
    )
    domain_ids = {a.domain_id for a in out}
    # Lithology in name → Geology; assay in name → Geochemistry;
    # downhole EM in name → Geophysics. All three present.
    assert DOMAIN_GEOLOGY in domain_ids
    assert DOMAIN_GEOCHEMISTRY in domain_ids
    assert DOMAIN_GEOPHYSICS in domain_ids


def test_content_snippet_can_add_signal() -> None:
    """Filename alone is ambiguous; content keyword tips classification."""
    out = classify_document(
        "project_data.pdf",
        content_snippet=(
            "Qualified Person: John Smith, P.Geo. "
            "This report is prepared in accordance with NI 43-101 ..."
        ),
    )
    reports = [a for a in out if a.domain_id == DOMAIN_REPORTS]
    assert reports
    assert reports[0].confidence >= 0.3
    assert any("content:" in p for p in reports[0].matched_patterns)


def test_unrelated_filename_returns_empty() -> None:
    """Falls back to no assignments — caller inserts 'unclassified' tag."""
    out = classify_document("invoice_2024_03.pdf")
    assert out == []


def test_assignments_sorted_by_confidence_desc() -> None:
    out = classify_document(
        "PLS-22-08_NI_43-101_drill_assay_summary.pdf",
        content_snippet="NI 43-101 ... soil sample collection ...",
    )
    confs = [a.confidence for a in out]
    assert confs == sorted(confs, reverse=True)


def test_extension_only_yields_weak_geophysics_signal() -> None:
    """A .las file with no other signal still gets a Geophysics tag at low confidence."""
    out = classify_document("anonymous_blob.las")
    geophys = [a for a in out if a.domain_id == DOMAIN_GEOPHYSICS]
    assert geophys
    assert geophys[0].confidence < 0.5  # extension-only is weak signal
