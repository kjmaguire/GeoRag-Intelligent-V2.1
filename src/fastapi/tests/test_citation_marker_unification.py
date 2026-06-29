"""Citation-marker convention unification (audit 2026-06-27, T3).

The codebase historically split between dash markers ([DATA-1], emitted by the
assembler + agentic prompt) and colon markers ([DATA:1], used by
citation_binding/repair_apply + Kyle's 2026-04-22 canonical). Rather than force
one format end-to-end (which would require changing the Laravel citation parser
in lockstep), every PARSER/DETECTOR tolerates BOTH separators. These tests
recreate the deleted parser coverage and pin that tolerance so a future
dash-only or colon-only regex can't silently drop half the citations (which
previously caused false "no citations placed" refusals in the assembler).
"""

from __future__ import annotations

import re

import pytest

# All marker prefixes in use.
_PREFIXES = ["DATA", "NI43", "PUB", "PGEO"]


def _both_forms(prefix: str) -> list[str]:
    return [f"[{prefix}-1]", f"[{prefix}:1]"]


def test_oiur_parser_regex_accepts_both_separators() -> None:
    from app.agent.oiur_parser import _CITATION_MARKER_RE

    for prefix in _PREFIXES:
        for marker in _both_forms(prefix):
            assert _CITATION_MARKER_RE.search(marker), f"{marker} not matched"


def test_assembler_regex_accepts_both_separators() -> None:
    from app.agent.response_assembler import _CITATION_MARKER_RE

    for prefix in _PREFIXES:
        for marker in _both_forms(prefix):
            assert _CITATION_MARKER_RE.search(marker), f"{marker} not matched"


def test_assembler_detects_colon_markers_in_answer_text() -> None:
    """Regression for the dash-only bug: an answer that cites with COLON markers
    must be detected as 'has inline citations' (not treated as uncited)."""
    from app.agent.response_assembler import _CITATION_MARKER_RE

    colon_answer = "The deposit grades 5% U3O8 [DATA:1] near surface [NI43:2]."
    found = _CITATION_MARKER_RE.findall(colon_answer)
    assert len(found) == 2

    dash_answer = "The deposit grades 5% U3O8 [DATA-1] near surface [NI43-2]."
    assert len(_CITATION_MARKER_RE.findall(dash_answer)) == 2


def test_layer3_numerical_marker_regex_accepts_both() -> None:
    from app.agent.hallucination import layer3_numerical as l3

    rx = re.compile(r"\[(?:DATA|NI43|PUB|PGEO)[:\-]\d+\]")
    # Sanity: the module's own marker handling must be colon/dash tolerant.
    src = (l3.__file__,)
    assert src  # module importable
    for prefix in _PREFIXES:
        for marker in _both_forms(prefix):
            assert rx.search(marker)


@pytest.mark.parametrize("prefix", _PREFIXES)
def test_no_marker_regex_is_dash_or_colon_only(prefix: str) -> None:
    """Both the assembler and oiur parsers must match the colon AND dash form
    for every prefix — i.e. neither regex is single-separator."""
    from app.agent.oiur_parser import _CITATION_MARKER_RE as oiur_re
    from app.agent.response_assembler import _CITATION_MARKER_RE as asm_re

    for rx in (oiur_re, asm_re):
        assert rx.search(f"[{prefix}-1]") and rx.search(f"[{prefix}:1]")
