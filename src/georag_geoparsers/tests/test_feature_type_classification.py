"""``_infer_feature_type`` must only ever return a legal feature_type.

WHY THIS TEST EXISTS
    Until 2026-08-20 this classifier returned "alteration", "target" and
    "feature". None of the three is in the CHECK constraint on
    silver.spatial_features, and "feature" was the DEFAULT -- so any
    spatial file whose features matched no rule failed its whole insert
    with

        new row for relation "spatial_features" violates check constraint
        "chk_spatial_features_type"

    The fix introduced FEATURE_TYPES and _TYPE_RULES. Nothing tested it.
    That is the gap this file closes, and it is the one place where the
    live parser has genuinely diverged from the frozen src/dagster copy
    the ported tests came from -- so it is exactly where the ported tests
    have nothing to say.

    None of this needs geopandas: spatial_parser imports the geospatial
    stack lazily, inside the read functions.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from georag_geoparsers.spatial_parser import (
    _TYPE_RULES,
    FEATURE_TYPES,
    _infer_feature_type,
)

#: The migration that owns the CHECK constraint. Resolved relative to this
#: file so the package keeps working if the repo layout moves; the test
#: skips rather than fails when the Laravel tree is not present, because
#: georag_geoparsers is installable on its own.
MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "database" / "migrations"
    / "2026_05_22_010000_extend_silver_spatial_features.php"
)


def test_every_rule_produces_a_declared_type() -> None:
    """The rule table cannot name a type FEATURE_TYPES does not contain."""
    for needles, canonical in _TYPE_RULES:
        assert canonical in FEATURE_TYPES, (
            f"_TYPE_RULES maps {needles!r} to {canonical!r}, which is not in "
            f"FEATURE_TYPES. Every value here reaches the feature_type column "
            f"directly, so an undeclared one is a failed insert, not a "
            f"mislabelled row."
        )


def test_default_is_a_declared_type() -> None:
    """The no-match path is the one that actually broke production."""
    assert _infer_feature_type({}, "unlabelled_survey.shp") in FEATURE_TYPES


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("mylonite_zone.shp", "shear_zone"),
        ("regional_shear.geojson", "shear_zone"),
        ("dolerite_dykes.shp", "dyke"),
        ("qtz_veins.shp", "mineralization_zone"),
        ("sericite_alteration.shp", "alteration_halo"),
        ("gossan_outline.shp", "alteration_halo"),
        ("magnetic_trend_lines.shp", "lineament"),
        ("unconformity_trace.shp", "contact"),
        ("thrust_faults.shp", "fault"),
        ("minfile_showings.shp", "occurrence"),
        ("soil_geochem_stations.shp", "sample_point"),
        ("bedrock_exposure.shp", "outcrop"),
        ("claim_boundary.shp", "boundary"),
    ],
)
def test_representative_filenames_classify(filename: str, expected: str) -> None:
    assert _infer_feature_type({}, filename) == expected


def test_shear_beats_fault() -> None:
    """Rule order is load-bearing, not incidental.

    "shearzone" appears in the fault rule as well, so a file named for a
    shear zone would classify as a plain fault if the rules were reordered
    alphabetically or by length. Shear zones and faults are different
    structures with different implications for a drill target.
    """
    assert _infer_feature_type({}, "main_shearzone.shp") == "shear_zone"


def test_exploration_target_does_not_become_a_mineralization_zone() -> None:
    """A target is a hypothesis; a mineralization zone is a claim of fact.

    Mapping one to the other would have the system assert mineralization
    where a geologist only proposed looking. "other" keeps the feature and
    its name without inventing the claim.
    """
    assert _infer_feature_type({}, "priority_targets_2026.shp") == "other"
    assert _infer_feature_type({"name": "Target A"}, "areas.shp") == "other"


def test_properties_are_read_not_just_the_filename() -> None:
    """A generic filename with typed attributes still classifies."""
    assert _infer_feature_type(
        {"structure": "Fault", "confidence": "inferred"}, "layer1.shp"
    ) == "fault"


def test_none_valued_properties_are_skipped() -> None:
    """A None must not stringify to "none" and match a rule by accident."""
    assert _infer_feature_type(
        {"note": None, "kind": None}, "unlabelled.shp"
    ) == "other"


def test_feature_types_matches_the_database_check_constraint() -> None:
    """The constant and the constraint are two copies of one list.

    They live in different languages in different trees, and the last time
    they disagreed every insert from an unmatched file failed. Nothing but
    this test connects them.
    """
    if not MIGRATION.is_file():
        pytest.skip(f"Laravel migration tree not present at {MIGRATION}")

    php = MIGRATION.read_text(encoding="utf-8")
    match = re.search(
        r"ADD CONSTRAINT chk_spatial_features_type\s+CHECK \(feature_type IN \((.*?)\)\)",
        php,
        re.DOTALL,
    )
    assert match, (
        "could not find the chk_spatial_features_type CHECK in "
        f"{MIGRATION.name} -- if the constraint moved, repoint this test "
        "rather than deleting it"
    )
    declared = set(re.findall(r"'([a-z_]+)'", match.group(1)))

    assert declared == set(FEATURE_TYPES), (
        "FEATURE_TYPES and the CHECK constraint have drifted.\n"
        f"  only in the constraint: {sorted(declared - set(FEATURE_TYPES))}\n"
        f"  only in FEATURE_TYPES:  {sorted(set(FEATURE_TYPES) - declared)}"
    )
