"""Unit tests for app.services.eval.benchmark_compare.

The diff logic is pure-function over two report dicts — no DB, no HTTP.
The CLI wrapper (scripts/compare_benchmarks.py) is a thin shell over
this module. The runner (scripts/run_golden_benchmark.py) drives live
vLLM + RAG infrastructure and is exercised by an integration smoke
during the baseline run rather than a unit test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.eval import benchmark_compare as compare_benchmarks

# ---------------------------------------------------------------------------
# Fixtures — minimal report builders
# ---------------------------------------------------------------------------


def _report(
    *,
    timestamp: str = "2026-05-29T16:30:00Z",
    git_sha: str = "abc1234",
    label: str | None = "baseline",
    results: list[dict] | None = None,
) -> dict:
    """Build a minimal report dict the comparison helpers consume."""
    results = results or []
    pass_count = sum(1 for r in results if r.get("passed"))
    fail_count = len(results) - pass_count
    return {
        "meta": {
            "timestamp": timestamp,
            "git_sha": git_sha,
            "label": label,
            "question_count": len(results),
        },
        "summary": {
            "pass_count": pass_count,
            "fail_count": fail_count,
            "pass_rate": round(pass_count / len(results), 4) if results else 0.0,
            "avg_latency_ms": 1000,
            "p95_latency_ms": 2000,
            "total_tokens": 1000 * len(results),
            "failure_layers": {"refusal": fail_count} if fail_count else {},
        },
        "results": results,
    }


def _q(qid: str, passed: bool, *, failure_layer: str | None = None,
       latency_ms: int = 1000) -> dict:
    """Build one per-question result entry."""
    return {
        "question_id": qid,
        "question_set": "test_set",
        "question_text_first_120": f"question {qid}?",
        "expected_refusal": False,
        "passed": passed,
        "failure_layer": failure_layer or (None if passed else "refusal"),
        "failure_detail_first_200": "" if passed else "expected_refusal=False but detected=True",
        "latency_ms": latency_ms,
        "tokens_used": 500,
    }


# ---------------------------------------------------------------------------
# _build_question_map
# ---------------------------------------------------------------------------


def test_build_question_map_indexes_by_question_id():
    rep = _report(results=[_q("a", True), _q("b", False)])
    m = compare_benchmarks.build_question_map(rep)
    assert set(m.keys()) == {"a", "b"}
    assert m["a"]["passed"] is True
    assert m["b"]["passed"] is False


# ---------------------------------------------------------------------------
# _diff_passes — partition logic
# ---------------------------------------------------------------------------


def test_diff_passes_regressed_when_was_passing_now_failing():
    before = compare_benchmarks.build_question_map(_report(results=[_q("a", True)]))
    after = compare_benchmarks.build_question_map(_report(results=[_q("a", False)]))
    regressed, improved, unchanged = compare_benchmarks.diff_passes(before, after)
    assert len(regressed) == 1 and regressed[0]["question_id"] == "a"
    assert improved == []
    assert unchanged == []


def test_diff_passes_improved_when_was_failing_now_passing():
    before = compare_benchmarks.build_question_map(_report(results=[_q("a", False)]))
    after = compare_benchmarks.build_question_map(_report(results=[_q("a", True)]))
    regressed, improved, unchanged = compare_benchmarks.diff_passes(before, after)
    assert improved and improved[0]["question_id"] == "a"
    assert regressed == []


def test_diff_passes_unchanged_when_both_pass_or_both_fail():
    before = compare_benchmarks.build_question_map(_report(
        results=[_q("a", True), _q("b", False)],
    ))
    after = compare_benchmarks.build_question_map(_report(
        results=[_q("a", True), _q("b", False)],
    ))
    regressed, improved, unchanged = compare_benchmarks.diff_passes(before, after)
    assert regressed == [] and improved == []
    assert len(unchanged) == 2


def test_diff_passes_skips_questions_only_in_one_run():
    """A question added/removed between runs is NOT a regression or
    improvement — it's incomparable. _diff_passes only walks questions
    in BOTH reports; the caller surfaces the asymmetric sets separately."""
    before = compare_benchmarks.build_question_map(_report(
        results=[_q("a", True), _q("b", True)],
    ))
    after = compare_benchmarks.build_question_map(_report(
        results=[_q("a", True), _q("c", True)],  # b dropped, c added
    ))
    regressed, improved, unchanged = compare_benchmarks.diff_passes(before, after)
    assert regressed == []
    assert improved == []
    assert [u["question_id"] for u in unchanged] == ["a"]  # only the shared id


def test_diff_passes_records_failure_layer_transitions():
    """The diff carries before+after failure_layer so reviewers can see
    'was failing on refusal, now failing on citation' — different bug."""
    before = compare_benchmarks.build_question_map(_report(
        results=[_q("a", False, failure_layer="refusal")],
    ))
    after = compare_benchmarks.build_question_map(_report(
        results=[_q("a", False, failure_layer="citation")],
    ))
    _, _, unchanged = compare_benchmarks.diff_passes(before, after)
    # Both failing → unchanged bucket, but failure_layers should differ
    assert len(unchanged) == 1
    assert unchanged[0]["before_failure_layer"] == "refusal"
    assert unchanged[0]["after_failure_layer"] == "citation"


# ---------------------------------------------------------------------------
# _diff_summary — top-line deltas
# ---------------------------------------------------------------------------


def test_diff_summary_pass_rate_delta():
    before = _report(results=[_q("a", True), _q("b", False)])  # 0.5
    after = _report(results=[_q("a", True), _q("b", True)])     # 1.0
    delta = compare_benchmarks.diff_summary(before, after)
    assert delta["pass_rate_delta"] == 0.5
    assert delta["pass_count_delta"] == 1


def test_diff_summary_regression_is_negative_delta():
    before = _report(results=[_q("a", True), _q("b", True)])    # 1.0
    after = _report(results=[_q("a", True), _q("b", False)])    # 0.5
    delta = compare_benchmarks.diff_summary(before, after)
    assert delta["pass_rate_delta"] == -0.5
    assert delta["pass_count_delta"] == -1


def test_diff_summary_latency_deltas_present_when_both_sides_have_them():
    before = _report(results=[_q("a", True)])
    after = _report(results=[_q("a", True)])
    delta = compare_benchmarks.diff_summary(before, after)
    assert delta["avg_latency_ms_delta"] == 0
    assert delta["p95_latency_ms_delta"] == 0


def test_diff_summary_latency_deltas_none_when_either_side_missing():
    before = _report(results=[_q("a", True)])
    before["summary"]["avg_latency_ms"] = None
    after = _report(results=[_q("a", True)])
    delta = compare_benchmarks.diff_summary(before, after)
    assert delta["avg_latency_ms_delta"] is None


# ---------------------------------------------------------------------------
# _load — happy + error paths
# ---------------------------------------------------------------------------


def test_load_parses_valid_report(tmp_path: Path):
    rep = _report(results=[_q("a", True)])
    p = tmp_path / "valid.json"
    p.write_text(json.dumps(rep))
    loaded = compare_benchmarks.load_report(p)
    assert loaded["summary"]["pass_rate"] == 1.0


def test_load_exits_on_missing_file(tmp_path: Path):
    with pytest.raises(SystemExit) as exc:
        compare_benchmarks.load_report(tmp_path / "does_not_exist.json")
    assert exc.value.code == 2


def test_load_exits_on_malformed_json(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")
    with pytest.raises(SystemExit) as exc:
        compare_benchmarks.load_report(p)
    assert exc.value.code == 2


def test_load_exits_on_missing_required_keys(tmp_path: Path):
    p = tmp_path / "incomplete.json"
    p.write_text('{"meta": {}}')  # missing 'summary' and 'results'
    with pytest.raises(SystemExit) as exc:
        compare_benchmarks.load_report(p)
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# _render_text — smoke
# ---------------------------------------------------------------------------


def test_render_text_includes_verdict_line_regression():
    before = _report(results=[_q("a", True)])
    after = _report(results=[_q("a", False)])
    regressed, improved, _ = compare_benchmarks.diff_passes(
        compare_benchmarks.build_question_map(before),
        compare_benchmarks.build_question_map(after),
    )
    delta = compare_benchmarks.diff_summary(before, after)
    text = compare_benchmarks.render_text(before, after, regressed, improved, delta)
    assert "VERDICT: REGRESSION" in text
    assert "REGRESSED" in text


def test_render_text_includes_verdict_line_improvement():
    before = _report(results=[_q("a", False)])
    after = _report(results=[_q("a", True)])
    regressed, improved, _ = compare_benchmarks.diff_passes(
        compare_benchmarks.build_question_map(before),
        compare_benchmarks.build_question_map(after),
    )
    delta = compare_benchmarks.diff_summary(before, after)
    text = compare_benchmarks.render_text(before, after, regressed, improved, delta)
    assert "VERDICT: IMPROVEMENT" in text
    assert "IMPROVED" in text


def test_render_text_neutral_when_no_change():
    before = _report(results=[_q("a", True)])
    after = _report(results=[_q("a", True)])
    regressed, improved, _ = compare_benchmarks.diff_passes(
        compare_benchmarks.build_question_map(before),
        compare_benchmarks.build_question_map(after),
    )
    delta = compare_benchmarks.diff_summary(before, after)
    text = compare_benchmarks.render_text(before, after, regressed, improved, delta)
    assert "VERDICT: NEUTRAL" in text
