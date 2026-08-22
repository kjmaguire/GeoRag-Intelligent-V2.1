"""Layer 3 was inert. These lock in that it is not.

Two independent defects, both of the same shape: a guard that exists, is
tested in isolation, and cannot fire on the case it was built for.

**Derivation tolerance.** `verify_numbers` accepted any number inside
`[min, max]` of the grounded set, and the grounded set is built by regexing
digit runs out of the JSON-serialised tool results -- ISO timestamps, UTM
eastings, UUID fragments. One realistic collar row gives a range of roughly
[-20, 512345.7], so every plausible geological value fell inside it and was
waved through as "likely average/median". The guard only ever fired on numbers
larger than the biggest coordinate in the payload.

**Unit families.** `_detect_unit_mismatches` flags a (value, unit) pair only
when every same-valued grounded tuple lives in a different unit family, and
the table put g/t, oz/t, ppm, ppb, wt% and % in one family called
"mass_conc". The entire class of grade-unit errors was therefore invisible by
construction -- including the g/t-versus-percent confusion the config comment
cites as the reason the guard was promoted from shadow to warn.
"""

from __future__ import annotations

import pytest

from app.agent.hallucination.orchestrator_validators import (
    _detect_unit_mismatches,
    _is_same_order_as_any,
)

#: A single realistic serialised collar row, as the grounded-number collector
#: actually sees it: a timestamp, a coordinate, a score and a page number.
NOISY_PAYLOAD_NUMBERS = [-20.0, 0.0, 11.0, 3.0, 512345.7, 0.82, 12.0, 2026.0, 8.0, 14.0]

#: Four real collar depths.
DEPTHS = [310.0, 360.0, 410.0, 455.0]


class TestDerivationTolerance:
    def test_a_genuine_mean_is_accepted(self) -> None:
        """The case the tolerance exists for: mean(DEPTHS) is 383.75."""
        assert _is_same_order_as_any(383.75, DEPTHS)

    @pytest.mark.parametrize("value", [4500.0, 12.0, 0.4])
    def test_a_value_of_the_wrong_scale_is_not_derived(self, value: float) -> None:
        """4,500 t of contained gold when the evidence says 45 kg."""
        assert not _is_same_order_as_any(value, DEPTHS)

    def test_coordinates_and_timestamps_no_longer_launder_a_fabrication(self) -> None:
        """The headline defect: 450 m invented against a payload of noise."""
        assert not _is_same_order_as_any(450.0, NOISY_PAYLOAD_NUMBERS)

    def test_magnitude_not_signed_range(self) -> None:
        """A -55 degree dip is judged against the 60 in the evidence."""
        assert _is_same_order_as_any(-55.0, [60.0])

    def test_zero_is_only_derived_from_zero(self) -> None:
        assert _is_same_order_as_any(0.0, [0.0, 310.0])
        assert not _is_same_order_as_any(0.0, [310.0, 455.0])

    def test_no_grounded_values_derives_nothing(self) -> None:
        assert not _is_same_order_as_any(383.75, [])


class TestUnitFamilies:
    @pytest.mark.parametrize(
        ("reported_unit", "why"),
        [
            ("%", "1.85% for 1.85 g/t is a 10,000x error"),
            ("ppb", "1.85 ppb for 1.85 g/t is a 1,000x error"),
            ("oz/t", "1.85 oz/t for 1.85 g/t is a ~34x error"),
        ],
    )
    def test_grade_unit_swaps_are_flagged(self, reported_unit: str, why: str) -> None:
        warnings = _detect_unit_mismatches(
            [(1.85, reported_unit)],
            [(1.85, "g/t")],
        )

        assert warnings, why

    def test_g_per_tonne_and_ppm_are_the_same_unit(self) -> None:
        """1 g/t IS 1 ppm. Flagging it would be a false positive."""
        assert not _detect_unit_mismatches([(1.85, "ppm")], [(1.85, "g/t")])

    def test_metres_reported_as_feet_is_flagged(self) -> None:
        assert _detect_unit_mismatches([(500.0, "ft")], [(500.0, "m")])

    def test_evidence_carrying_both_units_is_not_a_mismatch(self) -> None:
        assert not _detect_unit_mismatches(
            [(500.0, "m")],
            [(500.0, "m"), (500.0, "ft")],
        )

    def test_a_value_absent_from_the_evidence_is_left_to_the_grounding_check(self) -> None:
        """Not this guard's job to re-flag an ungrounded number."""
        assert not _detect_unit_mismatches([(12.5, "m")], [(1.85, "g/t")])

    def test_an_unrecognised_grounded_unit_is_not_treated_as_a_mismatch(self) -> None:
        assert not _detect_unit_mismatches([(1.85, "g/t")], [(1.85, "widgets")])
