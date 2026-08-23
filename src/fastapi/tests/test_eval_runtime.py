"""The eval runtime had zero tests.

All ten of its test files were deleted in 09d1d35 ("refactor(eval): remove
runtime quality harness", 2026-07-27). `app/services/eval/` was restored on
2026-08-14 — 2,183 lines of validator, evaluator, threshold and diff logic —
and none of the tests came back. The workflow that was supposed to protect it
against bit-rot had never executed.

This covers the pure functions, which need no live stack, so it runs in the
fast PR suite rather than behind the `-m "not golden"` exclusion. Four of the
five groups below pin behaviour that was broken as of 2026-08-21:

  * the nightly sampled the 10 alphabetically-first questions and never
    reached the §2.9 regulatory anchor set;
  * the nightly diffed a stubbed run against a 3-month-old real-LLM baseline
    from a different model stack, over a 2-question overlap;
  * `expected_language_compliance` was seeded, stored and hydrated but read
    by nothing, so the §2.9 set had no negative assertion;
  * `bench_retrieval_ndcg` reported a self-normalised DCG as nDCG@10.
"""

from __future__ import annotations

import importlib.util
import random
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.services.eval.benchmark_compare import comparability_report
from app.services.eval.validators import (
    _compliance_rules,
    _contains_phrase,
    detect_refusal,
    validate_language_compliance,
)

_FASTAPI_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _FASTAPI_ROOT.parent.parent
_SEED_YAML = _REPO_ROOT / "tests" / "golden_questions" / "seed_template.yaml"


def _load_script(name: str):
    """Import a scripts/ module by path.

    `scripts/` is not a package, and these two CLIs hold logic that is worth
    pinning: the question sampler and the retrieval metrics.
    """
    path = _FASTAPI_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_script_{name}", path)
    assert spec and spec.loader, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class FakeQuestion:
    """Stand-in for QuestionRecord carrying only the graded fields."""

    question_id: str = "q"
    question_set: str = "core_chat"
    expected_language_compliance: list[Any] = field(default_factory=list)
    expected_refusal: bool = False


# ---------------------------------------------------------------------------
# §2.9 language compliance — the anchor's missing negative assertion
# ---------------------------------------------------------------------------

BOUNDARY_RULES = [
    {"must_contain": "no public data within 25"},
    {"must_not_contain": ["PLS-22-08", "Triple R"]},
]


class TestLanguageCompliance:
    def test_a_refusal_that_leaks_anyway_fails(self) -> None:
        """The case `validate_refusal_correctness` structurally cannot see.

        That validator passes on the PRESENCE of a refusal phrase anywhere
        in the answer, so this text satisfies it completely.
        """
        leaky = (
            "I cannot share private drilling data from other workspaces. "
            "That said, hole PLS-22-08 reached 510 m."
        )

        outcome = validate_language_compliance(
            response_text=leaky,
            question=FakeQuestion(expected_language_compliance=BOUNDARY_RULES),
        )

        assert outcome.passed is False
        assert outcome.layer == "6_language"
        assert "PLS-22-08" in outcome.failure_message
        # Disclosure is reported before the missing-template half.
        assert outcome.failure_message.startswith("DISCLOSED")

    def test_the_template_answer_passes(self) -> None:
        outcome = validate_language_compliance(
            response_text=(
                "There is no public data within 25 km of the supplied "
                "coordinates."
            ),
            question=FakeQuestion(expected_language_compliance=BOUNDARY_RULES),
        )

        assert outcome.passed is True
        assert outcome.detail["checked"] is True

    def test_a_bare_refusal_without_the_template_fails(self) -> None:
        """§2.9 names a specific phrase; any-refusal-will-do is not the rule."""
        outcome = validate_language_compliance(
            response_text="I cannot answer that.",
            question=FakeQuestion(expected_language_compliance=BOUNDARY_RULES),
        )

        assert outcome.passed is False
        assert "missing required" in outcome.failure_message

    @pytest.mark.parametrize("response", ["", None])
    def test_an_empty_response_is_not_a_free_pass(self, response) -> None:
        outcome = validate_language_compliance(
            response_text=response,
            question=FakeQuestion(expected_language_compliance=BOUNDARY_RULES),
        )

        assert outcome.passed is False

    def test_a_question_with_no_rules_reports_that_it_was_not_checked(self) -> None:
        """Vacuous passes must be visible, not silent.

        `_layer_coverage` in run_golden_benchmark reads this to report how
        many questions each layer could actually grade.
        """
        outcome = validate_language_compliance(
            response_text="anything at all",
            question=FakeQuestion(),
        )

        assert outcome.passed is True
        assert outcome.detail["checked"] is False

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (["a"], (["a"], [])),
            ([{"must_contain": "a"}], (["a"], [])),
            ([{"must_not_contain": "b"}], ([], ["b"])),
            ([{"must_contain": ["a", "c"], "must_not_contain": ["b"]}],
             (["a", "c"], ["b"])),
            ([None, 42, {"junk": 1}], ([], [])),
            (None, ([], [])),
        ],
    )
    def test_rule_shapes(self, raw, expected) -> None:
        assert _compliance_rules(raw) == expected


class TestForbiddenPhraseMatching:
    """Word boundaries, because the sibling check in test_golden_queries
    is a bare substring test that bans 'RC' and '0'."""

    @pytest.mark.parametrize(
        ("text", "phrase"),
        [
            ("according to the source table", "rc"),
            ("search the record", "rc"),
            ("PLS-22-10 is in progress", "0"),
            ("drilled in 2020", "2"),
        ],
    )
    def test_a_short_token_does_not_match_inside_a_word(self, text, phrase) -> None:
        assert _contains_phrase(text.lower(), phrase) is False

    @pytest.mark.parametrize(
        ("text", "phrase"),
        [
            ("hole RC-22-01 was drilled", "rc"),
            ("no public data within 25 km", "25 km"),
            ("the grade was 5%", "5%"),
            ("hole PLS-22-08 reached 510 m", "PLS-22-08"),
        ],
    )
    def test_a_real_occurrence_still_matches(self, text, phrase) -> None:
        assert _contains_phrase(text.lower(), phrase.lower()) is True

    def test_an_empty_phrase_never_matches(self) -> None:
        assert _contains_phrase("anything", "") is False


# ---------------------------------------------------------------------------
# Refusal detection — the tail-disclaimer heuristic
# ---------------------------------------------------------------------------

class TestDetectRefusal:
    def test_a_plain_refusal_is_detected(self) -> None:
        assert detect_refusal("I cannot answer that.") is True

    def test_a_tail_disclaimer_on_a_substantive_answer_is_not_a_refusal(self) -> None:
        """The doc-phase 186 heuristic: last 20%, >=200 chars before it."""
        body = (
            "Hole PLS-22-08 was collared in 2022 and reached a total depth "
            "of 510 m, the deepest in the program. Nine of the ten holes are "
            "logged as Completed; PLS-22-10 remains In Progress. All ten are "
            "Diamond type, with eastings between 493,445 and 498,257. "
        )
        assert len(body) >= 200
        assert detect_refusal(body + "I can only answer geological questions.") is False

    def test_the_same_disclaimer_alone_is_a_refusal(self) -> None:
        assert detect_refusal("I can only answer geological questions.") is True

    def test_empty_text_is_not_a_refusal(self) -> None:
        assert detect_refusal("") is False


# ---------------------------------------------------------------------------
# Benchmark comparability
# ---------------------------------------------------------------------------

_STACK = {
    "llm_backend": "azure",
    "llm_deployment": "Cohere-command-a-plus-05-2026",
    "embedding_backend": "foundry",
    "embedding_model": "embed-v-4-0",
    "reranker_backend": "foundry",
    "reranker_model": "cohere-rerank-v4",
    "qdrant_collection": "georag_chunks",
}


def _report(stack: dict | None, ids: list[str]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if stack is not None:
        meta["model_stack"] = stack
    return {"meta": meta, "results": [{"question_id": i, "passed": True} for i in ids]}


class TestBenchmarkComparability:
    def test_two_runs_of_the_same_stack_are_comparable(self) -> None:
        ids = [f"q{i}" for i in range(10)]
        result = comparability_report(_report(_STACK, ids), _report(_STACK, ids))

        assert result["comparable"] is True
        assert result["reasons"] == []
        assert result["overlap"] == 10

    def test_a_report_with_no_model_stack_is_not_comparable(self) -> None:
        """Every artefact written before 2026-08-21, including the baseline
        the nightly was diffing against."""
        ids = [f"q{i}" for i in range(10)]
        result = comparability_report(_report(None, ids), _report(_STACK, ids))

        assert result["comparable"] is False
        assert any("no meta.model_stack" in r for r in result["reasons"])

    @pytest.mark.parametrize(
        "key",
        ["llm_deployment", "embedding_model", "reranker_model", "qdrant_collection"],
    )
    def test_one_changed_knob_blocks_the_diff_and_names_itself(self, key) -> None:
        ids = [f"q{i}" for i in range(10)]
        moved = {**_STACK, key: "something-else"}
        result = comparability_report(_report(_STACK, ids), _report(moved, ids))

        assert result["comparable"] is False
        assert any(r.startswith(f"{key}:") for r in result["reasons"])

    def test_a_two_question_overlap_is_refused(self) -> None:
        """The real overlap between ci_nightly and the committed baseline."""
        result = comparability_report(
            _report(_STACK, [f"q{i}" for i in range(20)]),
            _report(_STACK, ["q0", "q1"]),
        )

        assert result["comparable"] is False
        assert result["overlap"] == 2
        assert any("noise" in r for r in result["reasons"])

    def test_disjoint_question_sets_are_refused(self) -> None:
        result = comparability_report(
            _report(_STACK, ["a", "b"]), _report(_STACK, ["c", "d"]),
        )

        assert result["comparable"] is False
        assert result["overlap"] == 0
        assert any("share no question IDs" in r for r in result["reasons"])


# ---------------------------------------------------------------------------
# Stratified question sampling
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def bench():
    return _load_script("run_golden_benchmark")


@pytest.fixture(scope="module")
def corpus() -> list[FakeQuestion]:
    """The committed corpus, ordered as `_load_active_questions` returns it.

    question_id is uuid5(NAMESPACE_OID, yaml id) per GoldenQuestionsSeeder,
    and the query is `ORDER BY question_set, question_id`.
    """
    data = yaml.safe_load(_SEED_YAML.read_text(encoding="utf-8"))
    rows = [
        FakeQuestion(
            question_id=str(uuid.uuid5(uuid.NAMESPACE_OID, entry["id"])),
            question_set=entry["question_set"],
        )
        for entry in data["categories"]
    ]
    rows.sort(key=lambda r: (r.question_set, r.question_id))
    return rows


class TestStratifiedSampling:
    def test_the_old_head_slice_missed_the_regulatory_anchor(self, corpus) -> None:
        """Not a test of current behaviour — a record of what was wrong.

        `questions[: max_questions]` over a list ordered by
        (question_set, question_id) cannot reach a set that sorts late.
        """
        sets_hit = {q.question_set for q in corpus[:10]}

        assert "public_private_boundary" not in sets_hit
        assert "refusal_correctness" not in sets_hit

    def test_a_global_cap_is_now_round_robin_across_sets(self, bench, corpus) -> None:
        picked, meta = bench._select_questions(corpus, 10, None, bench._DEFAULT_SAMPLE_SEED)

        assert len(picked) == 10
        assert meta["strategy"] == "round_robin"
        assert "public_private_boundary" in meta["sets_represented"]

    def test_per_set_reaches_every_set(self, bench, corpus) -> None:
        picked, meta = bench._select_questions(corpus, None, 2, bench._DEFAULT_SAMPLE_SEED)

        assert meta["sets_unrepresented"] == []
        assert set(meta["sets_represented"]) == {q.question_set for q in corpus}
        assert meta["sets_represented"]["public_private_boundary"] == 2
        assert len(picked) == sum(meta["sets_represented"].values())

    def test_a_cap_on_top_of_per_set_stays_stratified(self, bench, corpus) -> None:
        picked, meta = bench._select_questions(corpus, 4, 2, bench._DEFAULT_SAMPLE_SEED)

        assert len(picked) == 4
        assert meta["strategy"] == "per_set+round_robin"
        # Four distinct sets, not four questions from one.
        assert len(meta["sets_represented"]) == 4

    def test_no_cap_returns_the_corpus_untouched(self, bench, corpus) -> None:
        picked, meta = bench._select_questions(corpus, None, None, 1)

        assert picked is corpus
        assert meta["strategy"] == "all"

    def test_the_same_seed_draws_the_same_sample(self, bench, corpus) -> None:
        a, _ = bench._select_questions(corpus, 10, None, 20260821)
        b, _ = bench._select_questions(corpus, 10, None, 20260821)

        assert [q.question_id for q in a] == [q.question_id for q in b]

    def test_a_different_seed_draws_a_different_sample(self, bench, corpus) -> None:
        a, _ = bench._select_questions(corpus, 10, None, 20260821)
        b, _ = bench._select_questions(corpus, 10, None, 7)

        assert [q.question_id for q in a] != [q.question_id for q in b]

    def test_sampling_does_not_disturb_global_random_state(self, bench, corpus) -> None:
        """Uses its own Random instance — a bench run must not reseed
        anything else in the process."""
        random.seed(1234)
        before = random.random()
        random.seed(1234)
        bench._select_questions(corpus, 10, None, 20260821)

        assert random.random() == before

    def test_an_unrepresented_set_is_reported_not_hidden(self, bench, corpus) -> None:
        _, meta = bench._select_questions(corpus, 2, None, bench._DEFAULT_SAMPLE_SEED)

        assert meta["sets_unrepresented"]


class TestBoundaryCorpus:
    """The §2.9 set is the anchor `thresholds.py` says it is."""

    @pytest.fixture(scope="class")
    def entries(self) -> list[dict]:
        data = yaml.safe_load(_SEED_YAML.read_text(encoding="utf-8"))
        return [
            e for e in data["categories"]
            if e["question_set"] == "public_private_boundary"
        ]

    def test_the_set_is_not_a_single_unrelated_question(self, entries) -> None:
        assert len(entries) >= 4

    def test_every_boundary_question_carries_a_language_rule(self, entries) -> None:
        for entry in entries:
            assert entry.get("expected_language_compliance"), entry["id"]

    def test_at_least_one_forbids_disclosing_withheld_content(self, entries) -> None:
        forbidden = [
            e for e in entries
            if any(
                isinstance(r, dict) and r.get("must_not_contain")
                for r in e["expected_language_compliance"]
            )
        ]

        assert forbidden, "a boundary set with no negative assertion is not a boundary"

    def test_the_2_9_template_phrase_is_required_somewhere(self, entries) -> None:
        required = {
            r["must_contain"]
            for e in entries
            for r in e["expected_language_compliance"]
            if isinstance(r, dict) and isinstance(r.get("must_contain"), str)
        }

        assert any("no public data within 25" in r for r in required)

    def test_ids_are_unique_across_the_whole_corpus(self) -> None:
        """uuid5 is derived from the id, so a duplicate silently collapses
        two questions into one row."""
        data = yaml.safe_load(_SEED_YAML.read_text(encoding="utf-8"))
        ids = [e["id"] for e in data["categories"]]

        assert len(ids) == len(set(ids)), [
            i for i, c in Counter(ids).items() if c > 1
        ]


# ---------------------------------------------------------------------------
# Retrieval metrics
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ndcg():
    return _load_script("bench_retrieval_ndcg")


class TestRetrievalMetrics:
    @pytest.mark.parametrize(
        ("name", "relevances"),
        [
            ("ten uniform keyword matches", [3.0] * 10),
            ("five matched, well ordered", [3.0, 3.0, 3.0, 2.0, 2.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
            ("one matched, nine empty", [3.0] + [0.0] * 9),
        ],
    )
    def test_any_non_increasing_vector_scores_a_perfect_one(
        self, ndcg, name, relevances,
    ) -> None:
        """Why the metric is not nDCG: the ideal ranking is the sorted copy
        of the retrieved slice, so it is its own denominator."""
        assert ndcg._self_normalised_dcg(relevances, 10) == 1.0, name

    def test_precision_separates_what_the_dcg_cannot(self, ndcg) -> None:
        one_hit = [3.0] + [0.0] * 9
        ten_hits = [3.0] * 10

        assert ndcg._self_normalised_dcg(one_hit, 10) == ndcg._self_normalised_dcg(ten_hits, 10)
        assert ndcg._precision_at_k(one_hit, 10) == 0.1
        assert ndcg._precision_at_k(ten_hits, 10) == 1.0

    def test_a_bad_ordering_is_penalised(self, ndcg) -> None:
        good = [3.0, 3.0, 2.0] + [0.0] * 7
        bad = list(reversed(good))

        assert ndcg._self_normalised_dcg(bad, 10) < ndcg._self_normalised_dcg(good, 10)
        assert ndcg._mrr(good) == 1.0
        assert ndcg._mrr(bad) == pytest.approx(1 / 8)

    def test_a_total_miss_scores_zero_everywhere(self, ndcg) -> None:
        miss = [0.0] * 10

        assert ndcg._self_normalised_dcg(miss, 10) == 0.0
        assert ndcg._mrr(miss) == 0.0
        assert ndcg._precision_at_k(miss, 10) == 0.0

    def test_the_degenerate_flag_fires_only_on_a_forced_one(self, ndcg) -> None:
        assert ndcg._is_degenerate([3.0] * 10) is True
        assert ndcg._is_degenerate([3.0, 2.0] + [0.0] * 8) is False
        # All-zero is honestly 0.0, not a forced 1.0.
        assert ndcg._is_degenerate([0.0] * 10) is False
        assert ndcg._is_degenerate([]) is False

    def test_empty_results_do_not_raise(self, ndcg) -> None:
        assert ndcg._self_normalised_dcg([], 10) == 0.0
        assert ndcg._mrr([]) == 0.0
        assert ndcg._precision_at_k([], 10) == 0.0

    @pytest.mark.parametrize(
        ("text", "substrings", "expected"),
        [
            ("contains U3O8 here", ["U3O8"], 3.0),
            ("contains u3o8 here", ["U3O8"], 2.0),
            ("nothing relevant", ["U3O8"], 0.0),
            ("anything", [], 0.0),
        ],
    )
    def test_the_substring_grader_is_unchanged(
        self, ndcg, text, substrings, expected,
    ) -> None:
        assert ndcg._grade(text, substrings) == expected
