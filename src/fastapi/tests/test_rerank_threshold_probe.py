"""The rerank-threshold probe: its math, its pairs, and a run through the real adapter.

`ops/validation/rerank_threshold_probe.py` produces the evidence a person
uses to decide RERANKER_SCORE_THRESHOLD_HOSTED, the system's only
retrieval-quality gate. So the tests here pin three different properties:

  1. The analysis is right on score distributions whose answer is known
     (synthetic, no service). A recommendation that is off by one grid step
     in the unsafe direction is the failure that matters.
  2. The pairs mean what they claim: the inverse-cloze query is cut OUT of
     its positive passage, and negatives come from other documents only.
  3. End to end, scoring goes through `get_reranker_or_none()` and the real
     `_BedrockReranker` request/response code, with only the boto3 client
     replaced (ops/validation/tests/fake_bedrock_rerank.py). A denied role,
     scores that depend on batch composition, and a model the pseudo-labels
     cannot separate must each come back as a refusal to recommend, not as
     a number.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

_OPS = Path(__file__).resolve().parents[3] / "ops" / "validation"
sys.path.insert(0, str(_OPS))
sys.path.insert(0, str(_OPS / "tests"))

import fake_bedrock_rerank  # noqa: E402
import rerank_threshold_probe as probe  # noqa: E402

# ---------------------------------------------------------------------------
# 1. The math, on distributions with a known answer
# ---------------------------------------------------------------------------


def _uniform(rng: random.Random, lo: float, hi: float, n: int) -> list[float]:
    return [rng.uniform(lo, hi) for _ in range(n)]


class TestPassRate:
    def test_it_matches_the_production_comparison(self) -> None:
        """tools.py keeps `score >= min_score`, so a score AT the floor passes."""
        assert probe.pass_rate([0.2, 0.19, 0.21], 0.2) == pytest.approx(2 / 3)


class TestAuc:
    def test_perfect_separation(self) -> None:
        assert probe.auc([0.9, 0.8], [0.1, 0.2]) == 1.0

    def test_reversed(self) -> None:
        assert probe.auc([0.1, 0.2], [0.9, 0.8]) == 0.0

    def test_ties_count_half(self) -> None:
        assert probe.auc([0.5, 0.5], [0.5, 0.5]) == 0.5

    def test_matches_brute_force(self) -> None:
        rng = random.Random(3)
        pos, neg = _uniform(rng, 0.2, 1.0, 40), _uniform(rng, 0.0, 0.6, 30)
        pos[0] = neg[0]  # force one tie
        brute = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))
        assert probe.auc(pos, neg) == pytest.approx(brute)


class TestEdges:
    def test_recall_edge_is_the_highest_value_keeping_the_target(self) -> None:
        on = [i / 100 for i in range(1, 101)]
        edge = probe.recall_edge(on, 0.95)
        assert edge == pytest.approx(0.06)
        assert probe.pass_rate(on, edge) >= 0.95
        assert probe.pass_rate(on, edge + 1e-9) < 0.95

    def test_edges_survive_float_rounding(self) -> None:
        """0.07 * 100 is 7.000000000000001 and 0.29 * 100 is 28.999999999999996.
        Without the epsilon, ceil() keeps 8 of 100 and floor() allows 28, and
        both edges move one sample in the wrong direction. Pin the exact counts."""
        scores = [i / 100 for i in range(100)]
        assert sum(s >= probe.recall_edge(scores, 0.07) for s in scores) == 7
        assert sum(s > probe.noise_edge(scores, 0.29) for s in scores) == 29

    def test_noise_edge_must_be_exceeded(self) -> None:
        off = [i / 100 for i in range(100)]
        edge = probe.noise_edge(off, 0.05)
        assert probe.pass_rate(off, edge + 1e-9) <= 0.05
        assert probe.pass_rate(off, edge) > 0.05


class TestRecommend:
    def test_current_value_inside_the_band_is_kept(self) -> None:
        rng = random.Random(0)
        rec = probe.recommend(
            _uniform(rng, 0.5, 0.95, 300),
            _uniform(rng, 0.0, 0.1, 300),
            _uniform(rng, 0.0, 0.02, 60),
            current=0.2,
        )
        assert rec["status"] == "current_consistent"
        assert rec["recommended"] == 0.2
        assert rec["band"][0] <= 0.2 <= rec["band"][1]
        assert rec["auc_on_vs_off_corpus"] == 1.0

    def test_a_leaky_floor_is_raised_by_the_smallest_step_that_fixes_it(self) -> None:
        rng = random.Random(0)
        on, off = _uniform(rng, 0.6, 1.0, 300), _uniform(rng, 0.0, 0.4, 300)
        rec = probe.recommend(on, off, [], current=0.2)
        assert rec["status"] == "current_outside_band"
        assert rec["direction"] == "raise"
        t = rec["recommended"]
        assert rec["at_recommended"]["off_topic_corpus_passed"] <= 0.05
        assert rec["at_recommended"]["on_topic_retained"] >= 0.95
        # Smallest change: the grid step below is still leaky.
        assert probe.pass_rate(off, round(t - 0.01, 2)) > 0.05

    def test_a_floor_that_drops_relevant_chunks_is_lowered(self) -> None:
        rng = random.Random(0)
        on, off = _uniform(rng, 0.1, 0.9, 300), _uniform(rng, 0.0, 0.03, 300)
        rec = probe.recommend(on, off, [], current=0.2)
        assert rec["status"] == "current_outside_band"
        assert rec["direction"] == "lower"
        assert rec["recommended"] < 0.2
        assert rec["at_recommended"]["on_topic_retained"] >= 0.95

    def test_overlap_is_resolved_recall_first(self) -> None:
        """No value meets both targets. The floor is the only gate, so the
        answer keeps on-topic retention and reports the leakage it costs."""
        rng = random.Random(0)
        on, off = _uniform(rng, 0.2, 1.0, 400), _uniform(rng, 0.0, 0.6, 400)
        rec = probe.recommend(on, off, [], current=0.5)
        assert rec["status"] == "overlap"
        assert rec["band"] is None
        assert rec["at_recommended"]["on_topic_retained"] >= 0.95
        assert probe.pass_rate(on, round(rec["recommended"] + 0.01, 2)) < 0.95
        assert rec["direction"] == "lower"

    def test_inseparable_populations_get_no_number(self) -> None:
        rng = random.Random(0)
        rec = probe.recommend(_uniform(rng, 0, 1, 300), _uniform(rng, 0, 1, 300), [], current=0.2)
        assert rec["status"] == "not_separable"
        assert rec["recommended"] is None

    def test_too_few_pairs_get_no_number(self) -> None:
        rec = probe.recommend([0.9] * 10, [0.1] * 10, [], current=0.2)
        assert rec["status"] == "insufficient_sample"
        assert rec["recommended"] is None

    def test_foreign_text_clearing_the_floor_is_flagged(self) -> None:
        rng = random.Random(0)
        rec = probe.recommend(_uniform(rng, 0.5, 0.95, 300), _uniform(rng, 0.0, 0.1, 300), [0.9] * 20, current=0.2)
        assert "warning" in rec

    def test_bootstrap_interval_brackets_the_point_estimate(self) -> None:
        rng = random.Random(0)
        on, off = _uniform(rng, 0.3, 1.0, 300), _uniform(rng, 0.0, 0.2, 300)
        rec = probe.recommend(on, off, [], current=0.2)
        lo, hi = rec["bootstrap"]["recall_edge_90ci"]
        assert lo <= rec["recall_edge"] <= hi
        lo, hi = rec["bootstrap"]["noise_edge_90ci"]
        assert lo <= rec["noise_edge"] <= hi


class TestMapThreshold:
    def test_a_monotone_rescaling_is_recovered(self) -> None:
        rng = random.Random(0)
        reference = [rng.random() for _ in range(500)]
        target = [r**2 for r in reference]
        mapped = probe.map_threshold(reference, target, reference_threshold=0.5)
        assert mapped["quantile_matched_threshold"] == pytest.approx(0.25, abs=0.01)
        assert mapped["agreement_max_threshold"] == pytest.approx(0.25, abs=0.011)
        assert mapped["agreement_at_best"] >= 0.99
        assert mapped["agreement_at_reference_threshold"] < 0.9

    def test_mismatched_lengths_are_an_error(self) -> None:
        assert "error" in probe.map_threshold([0.1], [0.1, 0.2], reference_threshold=0.2)


# ---------------------------------------------------------------------------
# 2. Pairs
# ---------------------------------------------------------------------------

_SUFFIXES = (
    "vein",
    "assay",
    "breccia",
    "sulphide",
    "schist",
    "intrusion",
    "fault",
    "grade",
    "collar",
    "adit",
    "shear",
    "porphyry",
)
_TOPICS = ("aurum", "cuprum", "argent", "zincite", "nickelo", "cobalto", "molyb", "lithio", "uranio", "stanno")


def _sentence(rng: random.Random, prefix: str) -> str:
    words = [f"{prefix}{s}" for s in rng.sample(_SUFFIXES, 8)]
    return "The " + " and ".join(words[:2]) + " with " + " ".join(words[2:]) + "."


def synthetic_corpus(documents: int = 10, per_document: int = 8, seed: int = 0) -> list[dict]:
    """Documents with disjoint vocabularies: a pair is on-topic iff it shares a document."""
    rng = random.Random(seed)
    corpus = []
    for d in range(documents):
        prefix = _TOPICS[d % len(_TOPICS)] + str(d // len(_TOPICS))
        for p in range(per_document):
            corpus.append(
                {
                    "passage_id": f"p-{d}-{p}",
                    "document_id": f"doc-{d}",
                    "text": " ".join(_sentence(rng, prefix) for _ in range(6)),
                }
            )
    return corpus


class TestPairs:
    def test_the_query_is_cut_out_of_its_positive_passage(self) -> None:
        pairs, stats = probe.build_corpus_pairs(
            synthetic_corpus(), max_anchors=20, negatives_per_anchor=3, foreign_per_anchor=1
        )
        assert stats["anchors_used"] == 20
        for pair in pairs:
            if pair.kind == probe.ON_TOPIC and pair.style == "sentence":
                assert pair.query not in pair.passage

    def test_negatives_come_from_other_documents_only(self) -> None:
        corpus = synthetic_corpus()
        doc_of_text = {c["text"][:8000]: c["document_id"] for c in corpus}
        pairs, _ = probe.build_corpus_pairs(corpus, max_anchors=20, negatives_per_anchor=3, foreign_per_anchor=1)
        groups: dict[str, dict[str, list]] = {}
        for pair in pairs:
            groups.setdefault(pair.group, {}).setdefault(pair.kind, []).append(pair)
        for members in groups.values():
            # The positive is its anchor minus one sentence, so find the
            # anchor by a sentence that survived.
            kept = probe.split_sentences(members[probe.ON_TOPIC][0].passage)[0]
            positive_doc = next(c["document_id"] for c in corpus if kept in c["text"])
            for negative in members[probe.OFF_TOPIC_CORPUS]:
                assert doc_of_text[negative.passage] != positive_doc
            assert all(p.passage in probe.FOREIGN_PASSAGES for p in members[probe.OFF_TOPIC_FOREIGN])

    def test_every_group_is_one_query_in_two_styles(self) -> None:
        pairs, _ = probe.build_corpus_pairs(
            synthetic_corpus(), max_anchors=5, negatives_per_anchor=2, foreign_per_anchor=1
        )
        assert {p.style for p in pairs} == {"sentence", "keywords"}
        for group in {p.group for p in pairs}:
            members = [p for p in pairs if p.group == group]
            assert len({p.query for p in members}) == 1
            assert [p.kind for p in members].count(probe.ON_TOPIC) == 1

    def test_the_same_seed_gives_the_same_pairs(self) -> None:
        corpus = synthetic_corpus()
        a, _ = probe.build_corpus_pairs(corpus, max_anchors=10, negatives_per_anchor=3, foreign_per_anchor=1, seed=7)
        b, _ = probe.build_corpus_pairs(corpus, max_anchors=10, negatives_per_anchor=3, foreign_per_anchor=1, seed=7)
        assert a == b

    def test_a_single_document_corpus_yields_no_corpus_negatives(self) -> None:
        corpus = synthetic_corpus(documents=1)
        pairs, stats = probe.build_corpus_pairs(corpus, max_anchors=5, negatives_per_anchor=3, foreign_per_anchor=1)
        assert stats["anchors_without_other_document"] == 5
        assert not [p for p in pairs if p.kind == probe.OFF_TOPIC_CORPUS]

    def test_keyword_query_drops_stopwords(self) -> None:
        assert probe.keyword_query("The gold grade of the vein is high within the shear zone") == (
            "gold grade vein high shear zone"
        )

    def test_pairs_file_rejects_an_unknown_kind(self, tmp_path: Path) -> None:
        f = tmp_path / "pairs.jsonl"
        f.write_text(json.dumps({"query": "q", "passage": "p", "kind": "relevant"}) + "\n")
        with pytest.raises(ValueError, match="kind must be one of"):
            probe.load_pairs_file(f)


# ---------------------------------------------------------------------------
# 2b. Harvest: the reranker.py note's route
# ---------------------------------------------------------------------------


class TestHarvest:
    ROWS = [
        {"answer_run_id": "r1", "refused": False, "reranker_score": 0.9},
        {"answer_run_id": "r1", "refused": False, "reranker_score": 0.35},
        {"answer_run_id": "r2", "refused": False, "reranker_score": 0.22},
        {"answer_run_id": "r3", "refused": True, "reranker_score": None},
    ]

    def test_null_versions_are_attributed_by_time_window_and_say_so(self) -> None:
        out = probe.analyze_harvest(
            self.ROWS,
            versions={None: 3},
            version="cohere-bedrock:cohere.rerank-v3-5:0",
            filtered_by_version=None,
            current=0.2,
            since="2026-09-08",
        )
        assert out["attribution"] == "time_window_assumed"
        assert "nothing writes that column" in out["attribution_note"]

    def test_the_distribution_is_flagged_as_truncated_at_the_floor(self) -> None:
        out = probe.analyze_harvest(
            self.ROWS, versions={None: 3}, version="v", filtered_by_version=None, current=0.2, since="x"
        )
        assert out["truncated_at_threshold"] is True
        assert out["runs"] == 3
        assert out["refusal_rate_without_chunks"] == 1.0
        assert out["refusal_rate_with_chunks"] == 0.0
        at_025 = next(r for r in out["raise_impact"] if r["threshold"] == 0.25)
        assert at_025["runs_emptied"] == 0.5  # r2's only chunk is 0.22
        assert at_025["chunks_dropped"] == pytest.approx(1 / 3, abs=1e-4)

    def test_no_runs_is_a_skip_not_evidence(self) -> None:
        out = probe.analyze_harvest([], versions={}, version="v", filtered_by_version=None, current=0.2, since="x")
        assert "skipped" in out


# ---------------------------------------------------------------------------
# 3. End to end, through the real adapter
# ---------------------------------------------------------------------------


@pytest.fixture
def production_path(monkeypatch):
    """Configure app.services.reranker as production has it, with a fake boto3 client.

    Everything from `get_reranker_or_none()` down (model id resolution, v4
    discovery, ARN construction, the request body, the index remap) is the
    real code. Only `_bedrock.get_client` is replaced.
    """
    from app.services import _bedrock
    from app.services import reranker as reranker_module

    def _use(mode: str = "separable"):
        fake_bedrock_rerank.reset()
        monkeypatch.setattr(reranker_module, "RERANKER_BACKEND", "bedrock")
        monkeypatch.setattr(reranker_module, "_BEDROCK_RERANK_MODEL_ID_EXPLICIT", False)
        monkeypatch.setattr(reranker_module, "BEDROCK_RERANK_MODEL_ID", fake_bedrock_rerank.V35)
        monkeypatch.setattr(
            _bedrock, "get_client", lambda service, **kw: fake_bedrock_rerank.client(service, mode=mode)
        )
        _bedrock.discover_cohere_rerank_v4_model_id.cache_clear()
        return fake_bedrock_rerank.client("bedrock-agent-runtime", mode=mode)

    yield _use
    _bedrock.discover_cohere_rerank_v4_model_id.cache_clear()
    fake_bedrock_rerank.reset()


def _args(**overrides) -> object:
    argv = ["--max-anchors", "40", "--min-pairs", "30", "--current-threshold", "0.2"]
    args = probe.build_parser().parse_args(argv)
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _run(**overrides) -> dict:
    corpus = synthetic_corpus()
    return probe.run(_args(**overrides), passage_loader=lambda **_kw: corpus)


class TestEndToEnd:
    def test_a_healthy_run_measures_and_recommends(self, production_path) -> None:
        fake = production_path("separable")
        report = _run()
        assert report["verdict"]["verified_anything"] is True
        assert report["reranker"]["version"] == "cohere-bedrock:cohere.rerank-v3-5:0"
        rec = report["recommendation"]
        assert rec["status"] == "current_consistent"
        assert rec["recommended"] == 0.2
        assert rec["auc_on_vs_off_corpus"] > 0.95
        assert report["stability"]["batch_independent"] is True
        # One Rerank call per query group, as production makes one per query,
        # plus one per stability re-score.
        groups = len({g for g in range(report["pairs"]["anchors_used"])}) * 2
        assert len(fake.calls) == groups + report["stability"]["pairs_rescored"]
        assert all(c["model_arn"].endswith("foundation-model/" + fake_bedrock_rerank.V35) for c in fake.calls)

    def test_the_report_carries_no_corpus_text(self, production_path) -> None:
        production_path("separable")
        serialized = json.dumps(_run(), default=str)
        for passage in synthetic_corpus():
            for sentence in probe.split_sentences(passage["text"]):
                assert sentence not in serialized
        assert "p-0-0" not in serialized and "doc-0" not in serialized

    def test_a_denied_role_is_not_evidence(self, production_path) -> None:
        fake = production_path("denied")
        report = _run()
        v = report["verdict"]
        assert v["verified_anything"] is False
        assert v["authentication_failed"] is True
        assert report["scoring"]["stopped_early"] is True
        assert report["recommendation"]["recommended"] is None
        assert report["scoring"]["batches"] == 3  # stopped after three, not all of them
        assert not fake.calls

    def test_batch_dependent_scores_are_caught(self, production_path) -> None:
        production_path("batch_dependent")
        report = _run()
        assert report["stability"]["batch_independent"] is False
        assert "warning_batch_dependence" in report["recommendation"]

    def test_a_model_that_cannot_separate_gets_no_number(self, production_path) -> None:
        production_path("noisy")
        rec = _run()["recommendation"]
        assert rec["status"] == "not_separable"
        assert rec["recommended"] is None

    def test_it_reports_the_model_it_actually_used(self, production_path) -> None:
        """If discovery switches to v4, the report must say v4, not the pinned 3.5."""
        fake = production_path("with_v4")
        report = _run()
        assert report["reranker"]["model_id"] == fake_bedrock_rerank.V4
        assert report["reranker"]["version"] == f"cohere-bedrock:{fake_bedrock_rerank.V4}"
        assert all(c["model_arn"].endswith(fake_bedrock_rerank.V4) for c in fake.calls)

    def test_the_wrong_backend_refuses(self, production_path, monkeypatch) -> None:
        from app.services import reranker as reranker_module

        production_path("separable")
        monkeypatch.setattr(reranker_module, "RERANKER_BACKEND", "cross_encoder")
        report = _run()
        assert report["reranker"]["error"]["type"] == "WrongBackend"
        assert report["verdict"]["verified_anything"] is False

    def test_a_reference_model_on_the_same_pairs_maps_the_threshold(self, production_path) -> None:
        production_path("separable")
        report = _run(reference_model_id=fake_bedrock_rerank.V35)
        mapping = report["reference"]["mapping"]
        assert mapping["pairs"] == report["scoring"]["pairs_scored"]
        assert mapping["agreement_at_reference_threshold"] == 1.0

    def test_harvest_runs_alongside(self, production_path) -> None:
        production_path("separable")
        report = probe.run(
            _args(harvest_since="2026-09-08"),
            passage_loader=lambda **_kw: synthetic_corpus(),
            harvest_loader=lambda **_kw: {
                "versions": {None: 3},
                "filtered_by_version": None,
                "rows": TestHarvest.ROWS,
            },
        )
        assert report["harvest"]["attribution"] == "time_window_assumed"
        assert "harvest" in report["verdict"]["sections_ok"]


class TestMain:
    def _pairs_file(self, tmp_path: Path) -> Path:
        corpus = synthetic_corpus()
        pairs, _ = probe.build_corpus_pairs(corpus, max_anchors=40, negatives_per_anchor=4, foreign_per_anchor=1)
        path = tmp_path / "pairs.jsonl"
        path.write_text(
            "\n".join(
                json.dumps({"query": p.query, "passage": p.passage, "kind": p.kind, "group": p.group}) for p in pairs
            ),
            encoding="utf-8",
        )
        return path

    def test_a_verified_run_writes_a_report_and_exits_zero(self, production_path, tmp_path: Path) -> None:
        production_path("separable")
        out = tmp_path / "reports"
        code = probe.main(
            [
                "--source",
                "pairs",
                "--pairs",
                str(self._pairs_file(tmp_path)),
                "--min-pairs",
                "30",
                "--current-threshold",
                "0.2",
                "--out",
                str(out),
            ]
        )
        assert code == 0
        written = list(out.glob("rerank_threshold_*.json"))
        assert len(written) == 1
        assert json.loads(written[0].read_text())["recommendation"]["status"] == "current_consistent"

    def test_a_run_that_measured_nothing_exits_nonzero(self, production_path, tmp_path: Path) -> None:
        production_path("denied")
        code = probe.main(
            ["--source", "pairs", "--pairs", str(self._pairs_file(tmp_path)), "--out", str(tmp_path / "r")]
        )
        assert code == 1
