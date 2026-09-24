"""Label-free measurement of RERANKER_SCORE_THRESHOLD_HOSTED against Rerank 3.5.

WHAT THIS MEASURES
    ``RERANKER_SCORE_THRESHOLD_HOSTED`` (0.2) is the only retrieval-quality
    gate in the system. It was measured against Cohere Rerank **v4** on
    2026-08-15 and carried over unvalidated to Rerank **3.5** on Bedrock
    (ADR-0022, ADR-0023). Calibrating it the textbook way needs
    (query, chunk, relevant?) triples, and there are none:
    ``tests/golden_questions/seed_template.yaml`` is a skeleton whose every
    ``expected_citations`` is empty and marked "SME fills".

    This script produces evidence about the floor WITHOUT anyone judging
    relevance. It never changes the threshold. It writes a report that a
    human reads before editing ``app/config.py``.

THE METHOD: corpus contrast (runs today, needs no traffic)
    1. Sample indexed passages from ``silver.document_passages``: the corpus
       the deployment actually retrieves from.
    2. For each anchor passage, take one of its own sentences as the query
       and remove that sentence from the passage. This is the inverse cloze
       task (Lee et al., 2019, ORQA): the passage is relevant to the query
       *by construction*, because the query was cut out of it. It is scored
       in two query styles: the sentence as written, and its content words
       only, the way people type keyword searches.
    3. Pair the same query with passages from OTHER documents (in-domain,
       off-topic) and with fixed foreign-domain texts (grossly off-topic).
    4. Score every pair through ``app.services.reranker.get_reranker_or_none()``,
       the same factory, adapter, model id resolution and call shape (one
       query and N documents per call) the query path uses. This is not a
       re-implementation of the wire call.
    5. Find the band of thresholds that keeps >= 95% of on-topic pairs and
       passes <= 5% of in-domain off-topic pairs (both targets are flags).
       The recommendation is the band point nearest the current value, so
       an in-band 0.2 comes back as "keep 0.2".

    WHY IT IS VALID WITHOUT LABELS. The labels are not judgements. They are
    facts about how each pair was built: a query cut from passage P is about
    P. A passage from a different report is, with high probability, not
    about it. Nobody is asked whether a chunk is relevant. What the
    reranker's scores on those two populations show is where the model puts
    "clearly related" and "clearly unrelated" on its own scale, which is
    exactly what a floor described as "clearly irrelevant" needs.

    WHERE IT IS BIASED, AND IN WHICH DIRECTION (written into every report):
      * Inverse-cloze positives are EASIER than real questions. The query
        shares vocabulary with its passage, so on-topic scores run high,
        and the band's upper edge is an OVERESTIMATE of the true one.
      * Cross-document negatives are sometimes on-topic: the same deposit,
        the same boilerplate in two NI 43-101 reports. Measured leakage is
        an UPPER bound on true leakage, so the band's lower edge is also
        biased high.
      Both biases point the same way. The measured band sits at or above
      the true one, so a threshold this probe calls too HIGH is too high in
      fact. A threshold it calls too LOW is only probably too low.

THE ROUTE IN app/services/reranker.py: harvest (needs traffic)
    The note there says: harvest ``answer_runs``, filter by
    ``reranker_version``, and pick the floor from the observed 3.5 score
    distribution against refusal outcomes. ``--harvest-since`` does that,
    and reports two facts about it that were found while building this
    script:
      * **Nothing writes ``answer_runs.reranker_version``.** The only
        INSERT (``app/agent/agentic_retrieval/nodes.py``, persist node) does
        not name the column, and ``app.state.reranker_version`` is computed
        at startup and never persisted. Every row is NULL, so a filter on
        the version returns nothing. The harvest falls back to the time
        window the operator gives and records that the attribution is an
        assertion, not a lookup.
      * **Only survivors are stored.** ``answer_retrieval_items`` rows with
        ``stage='reranked'`` are the chunks that PASSED the floor. The
        harvested distribution is cut off at the threshold that was in
        force, so it can show what raising the floor would drop but never
        what lowering it would recover. It is descriptive evidence, not a
        calibration.

OPTIONAL: model-to-model mapping
    ``--reference-model-id`` scores the identical pairs with a second
    Bedrock rerank model through the same adapter class, and reports which
    target threshold makes the same keep/drop decisions the reference makes
    at ``--reference-threshold``. That transfers a past decision to a new
    model. It says nothing about whether the past decision was right. Today
    Bedrock serves no v4 (see the 2026-09-16 probe report), so this leg is
    for the day ``discover_cohere_rerank_v4_model_id`` finds one, or for
    any future model swap.

WHAT THE REPORT NEVER CONTAINS
    No passage text, no query text, no passage ids. Only scores, counts and
    parameters. The report is meant to be committed, and the corpus is
    tenant data. Bedrock authenticates with the task role, so there is no
    key to leak either.

Usage (inside the VPC, via ops/rehearsal/run_rerank_threshold_probe.sh):
    PYTHONPATH=src/fastapi python ops/validation/rerank_threshold_probe.py \\
        --max-anchors 150 --out ops/validation/reports/
    # the note's route, once there is traffic:
    ... --harvest-since 2026-09-08
    # a hand-built pair set instead of the corpus (JSONL of
    # {"query", "passage", "kind": on_topic|off_topic_corpus|off_topic_foreign}):
    ... --source pairs --pairs my_pairs.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import re
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "fastapi"))

from _probe_verdict import compute_verdict  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ON_TOPIC = "on_topic"
OFF_TOPIC_CORPUS = "off_topic_corpus"
OFF_TOPIC_FOREIGN = "off_topic_foreign"
KINDS = (ON_TOPIC, OFF_TOPIC_CORPUS, OFF_TOPIC_FOREIGN)

#: Where the threshold lives today, used only when app.config cannot be
#: imported. The report records which one it used.
FALLBACK_THRESHOLD = 0.2

#: app/agent/tools.py truncates each passage to this many characters before
#: the hosted rerank call. The probe sends what production sends.
PASSAGE_CHAR_BUDGET = 8000

#: A threshold is a two-decimal config value, so the recommendation is
#: searched on that grid (plus the current value, whatever it is).
GRID = tuple(round(i * 0.01, 2) for i in range(101))

#: Tolerance for comparing fractions such as 57/60 against 0.95.
_EPS = 1e-9

#: Grossly off-topic passages. They mark what "clearly irrelevant" scores
#: look like on this model: if any of them clears the floor, the floor is
#: not filtering at all.
FOREIGN_PASSAGES = (
    "Preheat the oven to 180 degrees. Cream the butter and sugar until pale, then fold in the "
    "flour and bake for twenty-five minutes until a skewer comes out clean.",
    "Employees must submit expense claims within thirty days. Claims require an itemised "
    "receipt and the approval of a line manager before reimbursement is processed.",
    "The home side equalised in the eighty-ninth minute after a corner was flicked on at the "
    "near post, and the match finished level after four minutes of stoppage time.",
    "To reset the router, hold the recessed button for ten seconds until the status light "
    "blinks amber, then reconnect using the network name printed on the base.",
    "The novel follows two sisters who inherit a vineyard and must decide whether to sell it "
    "to a developer or restore the neglected vines themselves.",
    "Water the seedlings every morning and move them into a sunny window once the first true "
    "leaves appear. Harden them off for a week before planting outside.",
    "This software is provided as is, without warranty of any kind. In no event shall the "
    "authors be liable for any claim, damages or other liability.",
    "Boarding closes fifteen minutes before departure. Passengers with connecting flights "
    "should follow the transfer signs and present their boarding pass at security.",
)

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "there",
        "these",
        "this",
        "those",
        "to",
        "was",
        "were",
        "which",
        "with",
        "within",
        "will",
        "would",
        "than",
        "then",
        "also",
        "such",
        "not",
        "no",
        "can",
        "may",
        "other",
        "each",
        "all",
        "any",
        "both",
        "more",
        "most",
        "some",
        "very",
    ]
)

_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(])|\n\s*\n")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9\-]*")


# ---------------------------------------------------------------------------
# Pairs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pair:
    """One (query, passage) pair and how it was built. Text never leaves the process."""

    query: str
    passage: str
    kind: str
    group: str
    style: str = "supplied"


class Reranker(Protocol):
    def predict(self, pairs: list[tuple[str, str]]) -> list[float]: ...


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_BREAK.split(text or "") if s and s.strip()]


def _is_query_sentence(sentence: str) -> bool:
    """A sentence usable as a query: prose, not a table row or a page footer."""
    words = sentence.split()
    if not 6 <= len(words) <= 40:
        return False
    letters = sum(ch.isalpha() for ch in sentence)
    return letters / max(1, len(sentence)) >= 0.5


def keyword_query(sentence: str, max_terms: int = 6) -> str | None:
    """Content words of ``sentence`` in order: the way people type searches."""
    seen: list[str] = []
    for word in _WORD.findall(sentence):
        lowered = word.lower()
        if len(lowered) < 3 or lowered in _STOPWORDS or lowered in (w.lower() for w in seen):
            continue
        seen.append(word)
        if len(seen) >= max_terms:
            break
    return " ".join(seen) if len(seen) >= 3 else None


def build_corpus_pairs(
    passages: Sequence[dict[str, Any]],
    *,
    max_anchors: int,
    negatives_per_anchor: int,
    foreign_per_anchor: int,
    styles: Sequence[str] = ("sentence", "keywords"),
    seed: int = 0,
) -> tuple[list[Pair], dict[str, Any]]:
    """Turn corpus passages into on-topic and off-topic pairs. Pure; no I/O.

    ``passages`` are dicts carrying ``passage_id``, ``document_id`` and
    ``text``. The same seed and the same passages always give the same
    pairs, so two runs (or two models) can be compared pair for pair.
    """
    rng = random.Random(seed)
    order = list(passages)
    rng.shuffle(order)

    def doc_of(p: dict[str, Any]) -> str:
        # A passage with no document is its own document: never a negative
        # for itself, and never assumed to share a report with anything.
        return str(p.get("document_id") or f"passage:{p.get('passage_id')}")

    stats = {
        "passages_offered": len(order),
        "documents_offered": len({doc_of(p) for p in order}),
        "anchors_used": 0,
        "anchors_skipped_no_query_sentence": 0,
        "anchors_skipped_short_remainder": 0,
        "anchors_without_other_document": 0,
    }
    pairs: list[Pair] = []

    for anchor in order:
        if stats["anchors_used"] >= max_anchors:
            break
        sentences = split_sentences(str(anchor.get("text") or ""))
        candidates = [i for i, s in enumerate(sentences) if _is_query_sentence(s)]
        if not candidates:
            stats["anchors_skipped_no_query_sentence"] += 1
            continue
        pick = rng.choice(candidates)
        sentence = sentences[pick]
        remainder = " ".join(s for i, s in enumerate(sentences) if i != pick)
        if len(remainder) < 100:
            stats["anchors_skipped_short_remainder"] += 1
            continue

        anchor_doc = doc_of(anchor)
        pool = [p for p in order if doc_of(p) != anchor_doc and p.get("text")]
        if not pool:
            stats["anchors_without_other_document"] += 1
        negatives = rng.sample(pool, min(negatives_per_anchor, len(pool)))
        foreign = rng.sample(FOREIGN_PASSAGES, min(foreign_per_anchor, len(FOREIGN_PASSAGES)))

        index = stats["anchors_used"]
        stats["anchors_used"] += 1
        for style in styles:
            query = sentence if style == "sentence" else keyword_query(sentence)
            if not query:
                continue
            group = f"{index}:{style}"
            pairs.append(Pair(query, remainder[:PASSAGE_CHAR_BUDGET], ON_TOPIC, group, style))
            for neg in negatives:
                pairs.append(Pair(query, str(neg["text"])[:PASSAGE_CHAR_BUDGET], OFF_TOPIC_CORPUS, group, style))
            for text in foreign:
                pairs.append(Pair(query, text, OFF_TOPIC_FOREIGN, group, style))
    return pairs, stats


def load_pairs_file(path: Path) -> list[Pair]:
    """Read operator-supplied pairs: one JSON object per line."""
    pairs: list[Pair] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        kind = row.get("kind")
        if kind not in KINDS:
            raise ValueError(f"{path}:{lineno}: kind must be one of {KINDS}, got {kind!r}")
        query, passage = str(row.get("query") or ""), str(row.get("passage") or "")
        if not query or not passage:
            raise ValueError(f"{path}:{lineno}: query and passage are both required")
        group = str(row.get("group") or f"line{lineno}:{query}")
        pairs.append(Pair(query, passage[:PASSAGE_CHAR_BUDGET], kind, group, str(row.get("style") or "supplied")))
    return pairs


_PASSAGE_SQL = """
SELECT passage_id::text AS passage_id,
       document_id::text AS document_id,
       text
  FROM silver.document_passages
 WHERE embedding_id IS NOT NULL
   AND length(text) >= $1
 ORDER BY md5(passage_id::text || $2)
 LIMIT $3
"""


async def fetch_passages(dsn: str, *, limit: int, seed: int, min_chars: int = 300) -> list[dict[str, Any]]:
    """Sample indexed passages. Deterministic for a given seed and corpus.

    ``embedding_id IS NOT NULL`` keeps it to passages that are actually in
    the vector index, i.e. ones retrieval can return. No ``app.workspace_id``
    is set, so the sample spans workspaces. The text goes only to the
    reranker and never into the report.
    """
    import asyncpg  # noqa: PLC0415

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(_PASSAGE_SQL, min_chars, str(seed), limit)
    finally:
        await conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _err(exc: BaseException) -> dict[str, Any]:
    """Error shape shared with bedrock_probe.py; messages are truncated."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error", {})
        return {
            "type": type(exc).__name__,
            "code": error.get("Code"),
            "message": (error.get("Message") or "")[:400],
            "status": response.get("ResponseMetadata", {}).get("HTTPStatusCode"),
        }
    return {"type": type(exc).__name__, "message": str(exc)[:400]}


def score_pairs(
    reranker: Reranker,
    pairs: Sequence[Pair],
    *,
    groups_per_batch: int = 10,
    max_consecutive_failures: int = 3,
) -> tuple[list[float | None], dict[str, Any]]:
    """Score every pair. A failed batch leaves ``None`` rather than raising.

    Pairs are handed to ``predict`` a few query groups at a time; the Bedrock
    adapter then issues one Rerank call per query, as it does in production.
    Consecutive failures stop the run early: an expired token should cost
    three calls, not three hundred.
    """
    by_group: dict[str, list[int]] = {}
    for i, pair in enumerate(pairs):
        by_group.setdefault(pair.group, []).append(i)
    group_ids = list(by_group)

    scores: list[float | None] = [None] * len(pairs)
    section: dict[str, Any] = {
        "pairs_submitted": len(pairs),
        "pairs_scored": 0,
        "batches": 0,
        "batches_failed": 0,
        "stopped_early": False,
    }
    failures: list[dict[str, Any]] = []
    consecutive = 0
    started = time.monotonic()

    for start in range(0, len(group_ids), groups_per_batch):
        indices = [i for g in group_ids[start : start + groups_per_batch] for i in by_group[g]]
        section["batches"] += 1
        try:
            result = list(reranker.predict([(pairs[i].query, pairs[i].passage) for i in indices]))
            if len(result) != len(indices):
                raise ValueError(f"reranker returned {len(result)} scores for {len(indices)} pairs")
        except Exception as exc:  # noqa: BLE001 — recorded, never raised
            section["batches_failed"] += 1
            failures.append(_err(exc))
            consecutive += 1
            if consecutive >= max_consecutive_failures:
                section["stopped_early"] = True
                break
            continue
        consecutive = 0
        for i, value in zip(indices, result, strict=True):
            scores[i] = float(value)

    section["pairs_scored"] = sum(s is not None for s in scores)
    section["total_s"] = round(time.monotonic() - started, 3)
    if failures:
        section["first_failure"] = failures[0]
        section["distinct_failures"] = sorted({f"{f['type']}:{f.get('code')}" for f in failures})
    if section["pairs_scored"] == 0:
        # Nothing observed. Put the error where the shared verdict looks
        # for it, so this section can never read as evidence.
        section["error"] = failures[0] if failures else {"type": "NoPairs", "message": "nothing to score"}
    return scores, section


def stability_check(
    reranker: Reranker,
    pairs: Sequence[Pair],
    scores: Sequence[float | None],
    *,
    samples: int = 10,
    tolerance: float = 0.01,
) -> dict[str, Any]:
    """Re-score a few pairs ALONE and compare to their in-batch score.

    An absolute threshold assumes a pair's score does not depend on which
    other documents share the call. Production sends 40 candidates per call
    and this probe sends about six, so if that assumption fails, no floor
    measured here (or anywhere) transfers. It is cheap to check and costly
    to assume.

    Half the sample is off-topic on purpose. The first version re-scored
    on-topic pairs only, and a host that normalises by the best score in
    the call passed: the on-topic pair usually IS the best in its call, so
    it scores 1.0 in the batch and 1.0 alone.
    """
    on = [i for i, p in enumerate(pairs) if p.kind == ON_TOPIC and scores[i] is not None]
    off = [i for i, p in enumerate(pairs) if p.kind != ON_TOPIC and scores[i] is not None]
    chosen = [i for pair in zip(on, off, strict=False) for i in pair][:samples] or (on or off)[:samples]
    if not chosen:
        return {"skipped": "no scored pairs"}
    diffs: list[float] = []
    try:
        for i in chosen:
            alone = float(reranker.predict([(pairs[i].query, pairs[i].passage)])[0])
            diffs.append(abs(alone - float(scores[i])))  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        return {"error": _err(exc), "pairs_rescored": len(diffs)}
    worst = max(diffs)
    return {
        "pairs_rescored": len(diffs),
        "max_abs_diff": round(worst, 6),
        "tolerance": tolerance,
        "batch_independent": worst <= tolerance,
    }


# ---------------------------------------------------------------------------
# Analysis. Pure stdlib, so the math is testable without any service.
# ---------------------------------------------------------------------------


def pass_rate(scores: Sequence[float], threshold: float) -> float:
    """Fraction of ``scores`` the floor lets through: tools.py keeps ``score >= min_score``."""
    if not scores:
        return float("nan")
    return sum(s >= threshold for s in scores) / len(scores)


def quantile(sorted_scores: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile of an already-sorted sequence."""
    if not sorted_scores:
        return float("nan")
    pos = (len(sorted_scores) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_scores[lo] + (sorted_scores[hi] - sorted_scores[lo]) * (pos - lo)


def summarize(scores: Sequence[float]) -> dict[str, Any]:
    if not scores:
        return {"n": 0}
    s = sorted(scores)
    out: dict[str, Any] = {"n": len(s), "min": round(s[0], 6), "max": round(s[-1], 6)}
    for q in (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95):
        out[f"p{int(q * 100):02d}"] = round(quantile(s, q), 6)
    out["mean"] = round(sum(s) / len(s), 6)
    return out


def auc(positives: Sequence[float], negatives: Sequence[float]) -> float:
    """P(on-topic scores above off-topic): Mann-Whitney U, ties counted half.

    1.0 is perfect separation, 0.5 is a coin flip. Low AUC means either the
    model cannot tell the two apart or the pseudo-labels are bad. Either
    way no threshold drawn from them should be trusted.
    """
    if not positives or not negatives:
        return float("nan")
    tagged = sorted([(s, 1) for s in positives] + [(s, 0) for s in negatives])
    rank_sum = 0.0
    i = 0
    while i < len(tagged):
        j = i
        while j + 1 < len(tagged) and tagged[j + 1][0] == tagged[i][0]:
            j += 1
        mean_rank = (i + j + 2) / 2.0  # 1-based average rank across the tie run
        rank_sum += mean_rank * sum(tag for _, tag in tagged[i : j + 1])
        i = j + 1
    n_pos, n_neg = len(positives), len(negatives)
    return (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def recall_edge(positives: Sequence[float], target_retention: float) -> float:
    """The highest threshold that still keeps ``target_retention`` of on-topic pairs."""
    s = sorted(positives)
    keep = math.ceil(target_retention * len(s) - _EPS)
    return s[len(s) - keep] if keep else float("inf")


def noise_edge(negatives: Sequence[float], max_leakage: float) -> float:
    """The off-topic score a threshold must EXCEED to pass at most ``max_leakage``."""
    s = sorted(negatives)
    allowed = math.floor(max_leakage * len(s) + _EPS)
    return s[len(s) - allowed - 1] if allowed < len(s) else float("-inf")


def bootstrap_edges(
    positives: Sequence[float],
    negatives: Sequence[float],
    *,
    target_retention: float,
    max_leakage: float,
    rounds: int = 200,
    seed: int = 0,
) -> dict[str, list[float]]:
    """90% bootstrap intervals for both band edges: how much the sample size matters."""
    rng = random.Random(seed)
    recall, noise = [], []
    for _ in range(rounds):
        recall.append(recall_edge(rng.choices(positives, k=len(positives)), target_retention))
        noise.append(noise_edge(rng.choices(negatives, k=len(negatives)), max_leakage))
    recall.sort()
    noise.sort()
    return {
        "recall_edge_90ci": [round(quantile(recall, 0.05), 6), round(quantile(recall, 0.95), 6)],
        "noise_edge_90ci": [round(quantile(noise, 0.05), 6), round(quantile(noise, 0.95), 6)],
        "rounds": rounds,
    }


def threshold_row(
    t: float,
    on: Sequence[float],
    off_corpus: Sequence[float],
    off_foreign: Sequence[float],
) -> dict[str, Any]:
    def r(x: float) -> float | None:
        return None if math.isnan(x) else round(x, 4)

    return {
        "threshold": t,
        "on_topic_retained": r(pass_rate(on, t)),
        "off_topic_corpus_passed": r(pass_rate(off_corpus, t)),
        "off_topic_foreign_passed": r(pass_rate(off_foreign, t)),
    }


def recommend(
    on: Sequence[float],
    off_corpus: Sequence[float],
    off_foreign: Sequence[float],
    *,
    current: float,
    target_retention: float = 0.95,
    max_leakage: float = 0.05,
    min_pairs: int = 50,
    min_auc: float = 0.75,
    bootstrap_rounds: int = 200,
    seed: int = 0,
) -> dict[str, Any]:
    """Turn the three score populations into a recommendation with its evidence.

    Statuses:
      insufficient_sample   too few pairs to say anything; no recommendation.
      not_separable         AUC below ``min_auc``; no recommendation.
      current_consistent    the current value is inside the acceptable band.
      current_outside_band  it is not. The recommendation is the nearest
                            band point (the smallest change the evidence
                            supports), with the direction.
      overlap               no threshold meets both targets. The
                            recommendation is recall-first (the highest
                            value still keeping ``target_retention`` of
                            on-topic pairs), because this floor is the ONLY
                            gate: a relevant chunk it drops is gone, and the
                            run may refuse with no other signal, while an
                            off-topic chunk it lets through still has to
                            survive citation validation downstream.
    """
    out: dict[str, Any] = {
        "current": current,
        "targets": {"on_topic_retention_min": target_retention, "off_topic_corpus_leakage_max": max_leakage},
        "sample": {"on_topic": len(on), "off_topic_corpus": len(off_corpus), "off_topic_foreign": len(off_foreign)},
        "recommended": None,
    }
    if len(on) < min_pairs or len(off_corpus) < min_pairs:
        out["status"] = "insufficient_sample"
        out["reason"] = f"need >= {min_pairs} on-topic and >= {min_pairs} off-topic-corpus scores"
        return out

    separation = auc(on, off_corpus)
    out["auc_on_vs_off_corpus"] = round(separation, 4)
    out["auc_on_vs_off_foreign"] = round(auc(on, off_foreign), 4) if off_foreign else None
    out["recall_edge"] = round(recall_edge(on, target_retention), 6)
    out["noise_edge"] = round(noise_edge(off_corpus, max_leakage), 6)
    out["bootstrap"] = bootstrap_edges(
        on,
        off_corpus,
        target_retention=target_retention,
        max_leakage=max_leakage,
        rounds=bootstrap_rounds,
        seed=seed,
    )
    out["at_current"] = threshold_row(current, on, off_corpus, off_foreign)

    grid = sorted(set(GRID) | {current})
    band = [
        t
        for t in grid
        if pass_rate(on, t) >= target_retention - _EPS and pass_rate(off_corpus, t) <= max_leakage + _EPS
    ]
    out["band"] = [band[0], band[-1]] if band else None

    # Youden's J on the pseudo-labels, reported as context rather than as
    # the answer: it weighs a dropped relevant chunk and a leaked irrelevant
    # one equally, which this floor's job does not.
    youden = max(grid, key=lambda t: (pass_rate(on, t) - pass_rate(off_corpus, t), -abs(t - current)))
    out["youden_threshold"] = youden

    if separation < min_auc:
        out["status"] = "not_separable"
        out["reason"] = (
            f"AUC {separation:.3f} < {min_auc}: the model does not separate the pseudo-labelled "
            "populations, so no threshold drawn from them is trustworthy"
        )
        return out

    if band:
        if current in band:
            out["status"] = "current_consistent"
            out["recommended"] = current
        else:
            nearest = min(band, key=lambda t: (abs(t - current), t))
            out["status"] = "current_outside_band"
            out["recommended"] = nearest
            out["direction"] = "raise" if nearest > current else "lower"
    else:
        out["status"] = "overlap"
        out["recommended"] = max(t for t in grid if pass_rate(on, t) >= target_retention - _EPS)
        out["direction"] = (
            "raise" if out["recommended"] > current else "lower" if out["recommended"] < current else "keep"
        )
    out["at_recommended"] = threshold_row(out["recommended"], on, off_corpus, off_foreign)
    if out["at_recommended"]["off_topic_foreign_passed"]:
        out["warning"] = (
            "grossly off-topic foreign-domain passages clear the recommended value; the model's "
            "scale is not what this report assumes. Read the distributions before acting."
        )
    return out


def map_threshold(
    reference: Sequence[float],
    target: Sequence[float],
    *,
    reference_threshold: float,
) -> dict[str, Any]:
    """Which target threshold reproduces the reference model's keep/drop decisions?

    ``reference`` and ``target`` are scores for the SAME pairs, in the same
    order. Two answers are given because they can disagree:
      * quantile-matched: the target value that passes the same NUMBER of
        pairs the reference passes at ``reference_threshold``;
      * agreement-max: the grid value that makes the same decision on the
        most individual pairs.
    """
    if len(reference) != len(target) or not reference:
        return {"error": {"type": "ValueError", "message": "need equal-length, non-empty score lists"}}
    passed = sum(r >= reference_threshold for r in reference)
    desc = sorted(target, reverse=True)
    quantile_matched = desc[passed - 1] if passed else None

    def agreement(t: float) -> float:
        return sum((r >= reference_threshold) == (g >= t) for r, g in zip(reference, target, strict=True)) / len(
            reference
        )

    anchor = quantile_matched if quantile_matched is not None else reference_threshold
    best = max(GRID, key=lambda t: (agreement(t), -abs(t - anchor)))
    return {
        "pairs": len(reference),
        "reference_threshold": reference_threshold,
        "reference_pass_fraction": round(passed / len(reference), 4),
        "quantile_matched_threshold": None if quantile_matched is None else round(quantile_matched, 6),
        "agreement_max_threshold": best,
        "agreement_at_best": round(agreement(best), 4),
        "agreement_at_reference_threshold": round(agreement(reference_threshold), 4),
    }


# ---------------------------------------------------------------------------
# Harvest: the route app/services/reranker.py records
# ---------------------------------------------------------------------------

_HARVEST_VERSIONS_SQL = """
SELECT reranker_version, count(*)::bigint AS runs
  FROM silver.answer_runs
 WHERE created_at >= $1
 GROUP BY reranker_version
"""

_HARVEST_ROWS_SQL = """
SELECT ar.answer_run_id::text AS answer_run_id,
       (ar.rejection_reason IS NOT NULL) AS refused,
       ari.reranker_score::float8 AS reranker_score
  FROM silver.answer_runs AS ar
  LEFT JOIN silver.answer_retrieval_items AS ari
    ON ari.answer_run_id = ar.answer_run_id
   AND ari.stage = 'reranked'
   AND ari.source_store = 'qdrant'
 WHERE ar.created_at >= $1
   AND ($2::text IS NULL OR ar.reranker_version = $2)
"""


async def fetch_harvest(dsn: str, *, since: datetime, version: str) -> dict[str, Any]:
    """Read answer_runs + their surviving reranked chunks since ``since``."""
    import asyncpg  # noqa: PLC0415

    conn = await asyncpg.connect(dsn)
    try:
        versions = {r["reranker_version"]: int(r["runs"]) for r in await conn.fetch(_HARVEST_VERSIONS_SQL, since)}
        filter_version: str | None = version if versions.get(version) else None
        rows = [dict(r) for r in await conn.fetch(_HARVEST_ROWS_SQL, since, filter_version)]
    finally:
        await conn.close()
    return {"versions": versions, "filtered_by_version": filter_version, "rows": rows}


def analyze_harvest(
    rows: Iterable[dict[str, Any]],
    *,
    versions: dict[str | None, int],
    version: str,
    filtered_by_version: str | None,
    current: float,
    since: str,
) -> dict[str, Any]:
    """Describe what production traffic shows. Pure; see the module docstring for its limits."""
    runs: dict[str, dict[str, Any]] = {}
    for row in rows:
        run = runs.setdefault(row["answer_run_id"], {"refused": bool(row["refused"]), "scores": []})
        score = row.get("reranker_score")
        # Only the hosted scale. A cross_encoder-era row stores a sigmoided
        # logit, also in [0,1], which is why the time window matters.
        if score is not None and 0.0 <= float(score) <= 1.0:
            run["scores"].append(float(score))

    out: dict[str, Any] = {
        "since": since,
        "runs_by_reranker_version": {str(k): v for k, v in versions.items()},
        "version_wanted": version,
    }
    if filtered_by_version:
        out["attribution"] = "reranker_version"
    else:
        out["attribution"] = "time_window_assumed"
        out["attribution_note"] = (
            f"no answer_runs row since {since} carries reranker_version={version!r}; nothing writes that "
            "column today (the persist node's INSERT omits it). Rows are attributed to Rerank 3.5 by time "
            "window only. That is the operator's assertion, not a lookup."
        )
    if not runs:
        out["skipped"] = f"no answer_runs since {since}"
        return out

    with_chunks = [r for r in runs.values() if r["scores"]]
    without_chunks = [r for r in runs.values() if not r["scores"]]
    top1 = [max(r["scores"]) for r in with_chunks]
    every = [s for r in with_chunks for s in r["scores"]]

    def refusal_rate(group: list[dict[str, Any]]) -> float | None:
        return round(sum(r["refused"] for r in group) / len(group), 4) if group else None

    out.update(
        {
            "runs": len(runs),
            "runs_with_reranked_chunks": len(with_chunks),
            "runs_without_reranked_chunks": len(without_chunks),
            "refusal_rate_with_chunks": refusal_rate(with_chunks),
            "refusal_rate_without_chunks": refusal_rate(without_chunks),
            "top1_score": summarize(top1),
            "all_surviving_scores": summarize(every),
            "min_observed_score": round(min(every), 6) if every else None,
            # Every stored score passed the floor in force when it ran, so the
            # distribution is cut off there. Raising can be read off it;
            # lowering cannot.
            "truncated_at_threshold": bool(every) and min(every) >= current - 1e-6,
            "raise_impact": [
                {
                    "threshold": t,
                    "runs_emptied": round(sum(x < t for x in top1) / len(top1), 4) if top1 else None,
                    "chunks_dropped": round(sum(x < t for x in every) / len(every), 4) if every else None,
                }
                for t in (0.25, 0.3, 0.35, 0.4, 0.5, 0.6)
                if t > current
            ],
        }
    )
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

_EVIDENCE_SECTIONS = ("scoring", "harvest")

_AUTH_CODES = frozenset(
    {"UnrecognizedClientException", "InvalidClientTokenId", "AccessDeniedException", "ExpiredTokenException"}
)


def _is_auth_failure(section: dict[str, Any]) -> bool:
    return any((section.get(key) or {}).get("code") in _AUTH_CODES for key in ("error", "first_failure"))


def verdict(report: dict[str, Any]) -> dict[str, Any]:
    """Whether this run observed anything: the shared rule in _probe_verdict.py."""
    return compute_verdict(
        report,
        sections=_EVIDENCE_SECTIONS,
        is_auth_failure=_is_auth_failure,
        auth_hint="Check the task role's bedrock:Rerank and bedrock:InvokeModel grants and the region.",
    )


STATIC_CAVEATS = (
    "Inverse-cloze positives share vocabulary with their passage, so they score HIGHER than real questions "
    "would. The band's upper edge (recall_edge) is an overestimate.",
    "Cross-document negatives are sometimes genuinely on-topic (same deposit, same report boilerplate), so "
    "measured leakage is an upper bound. The band's lower edge (noise_edge) is biased high too.",
    "Both biases point up. A current value this report calls too HIGH is too high. One it calls too LOW is only "
    "probably too low.",
    "This measures where Rerank 3.5 places 'clearly related' and 'clearly unrelated' on its own scale. It does "
    "not measure answer quality. SME-labelled relevance remains the real calibration when it exists.",
    "The report is evidence for a human edit to app/config.py. It changes nothing by itself.",
)


def _current_threshold(override: float | None) -> tuple[float, str]:
    if override is not None:
        return override, "--current-threshold"
    try:
        from app.config import settings  # noqa: PLC0415

        return float(settings.RERANKER_SCORE_THRESHOLD_HOSTED), "app.config.settings.RERANKER_SCORE_THRESHOLD_HOSTED"
    except Exception as exc:  # noqa: BLE001
        return FALLBACK_THRESHOLD, f"fallback constant (app.config unavailable: {type(exc).__name__})"


def _production_reranker() -> tuple[Reranker | None, dict[str, Any]]:
    """The reranker the query path would get, and what it is."""
    from app.services import reranker as reranker_module  # noqa: PLC0415

    info: dict[str, Any] = {"backend": reranker_module.RERANKER_BACKEND}
    if reranker_module.RERANKER_BACKEND != "bedrock":
        info["error"] = {
            "type": "WrongBackend",
            "message": (
                f"RERANKER_BACKEND={reranker_module.RERANKER_BACKEND!r}: RERANKER_SCORE_THRESHOLD_HOSTED gates "
                "only the bedrock backend, whose scores are on a different scale from the self-hosted logits"
            ),
        }
        return None, info
    instance = reranker_module.get_reranker_or_none()
    # After get_reranker_or_none(), so a discovered v4 id is what is reported.
    info["version"] = reranker_module.active_reranker_version()
    info["model_id"] = reranker_module.BEDROCK_RERANK_MODEL_ID
    if instance is None:
        info["error"] = {"type": "NoReranker", "message": "get_reranker_or_none() returned None"}
    return instance, info


def _reference_reranker(model_id: str) -> Reranker:
    from app.services import reranker as reranker_module  # noqa: PLC0415

    return reranker_module._BedrockReranker(model_id, reranker_module.BEDROCK_RERANK_TIMEOUT_S)


def _scores_by_kind(pairs: Sequence[Pair], scores: Sequence[float | None]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {k: [] for k in KINDS}
    for pair, score in zip(pairs, scores, strict=True):
        if score is not None:
            out[pair.kind].append(score)
    return out


def run(
    args: argparse.Namespace,
    *,
    reranker_factory: Callable[[], tuple[Reranker | None, dict[str, Any]]] = _production_reranker,
    reference_factory: Callable[[str], Reranker] = _reference_reranker,
    passage_loader: Callable[..., Any] | None = None,
    harvest_loader: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Build the report. Loaders and factories are injectable for tests."""
    current, current_source = _current_threshold(args.current_threshold)
    report: dict[str, Any] = {
        "probe": "rerank_threshold",
        "probed_at": datetime.now(UTC).isoformat(),
        "region": os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION"),
        "current_threshold": {"value": current, "source": current_source},
        "method": {
            "name": "corpus_contrast_inverse_cloze",
            "source": args.source,
            "seed": args.seed,
            "max_anchors": args.max_anchors,
            "negatives_per_anchor": args.negatives_per_anchor,
            "foreign_per_anchor": args.foreign_per_anchor,
            "target_retention": args.target_retention,
            "max_leakage": args.max_leakage,
            "passage_char_budget": PASSAGE_CHAR_BUDGET,
            "report_contains_text": False,
        },
    }

    # ── Pairs ──────────────────────────────────────────────────────────────
    pairs: list[Pair] = []
    if args.source == "pairs":
        pairs = load_pairs_file(args.pairs)
        report["pairs"] = {"source": "file"}
    elif args.source == "corpus":
        loader = passage_loader or (lambda **kw: asyncio.run(fetch_passages(_dsn(), **kw)))
        try:
            passages = loader(limit=max(200, args.max_anchors * 4), seed=args.seed)
        except Exception as exc:  # noqa: BLE001
            report["pairs"] = {"source": "corpus", "error": _err(exc)}
        else:
            pairs, stats = build_corpus_pairs(
                passages,
                max_anchors=args.max_anchors,
                negatives_per_anchor=args.negatives_per_anchor,
                foreign_per_anchor=args.foreign_per_anchor,
                seed=args.seed,
            )
            report["pairs"] = {"source": "corpus", **stats}
    else:
        report["pairs"] = {"source": "none"}
    report["pairs"]["by_kind"] = {k: sum(p.kind == k for p in pairs) for k in KINDS}
    report["pairs"]["by_style"] = {s: sum(p.style == s for p in pairs) for s in sorted({p.style for p in pairs})}

    # ── Scoring through the production adapter ─────────────────────────────
    if not pairs:
        report["reranker"] = {}
        report["scoring"] = (
            {"skipped": "no pairs to score"}
            if args.source == "none"
            else {
                "error": (
                    report["pairs"].get("error") or {"type": "NoPairs", "message": "pair construction yielded none"}
                )
            }
        )
        scores: list[float | None] = []
    else:
        try:
            instance, info = reranker_factory()
        except Exception as exc:  # noqa: BLE001 (e.g. RetiredAzureConfiguration at import)
            instance, info = None, {"error": _err(exc)}
        report["reranker"] = info
        if instance is None:
            report["scoring"] = {"error": info.get("error") or {"type": "NoReranker"}}
            scores = [None] * len(pairs)
        else:
            scores, report["scoring"] = score_pairs(instance, pairs)
            report["stability"] = (
                stability_check(instance, pairs, scores, samples=args.stability_samples)
                if report["scoring"]["pairs_scored"]
                else {"skipped": "nothing scored"}
            )

    by_kind = _scores_by_kind(pairs, scores) if pairs else {k: [] for k in KINDS}
    report["distributions"] = {k: summarize(v) for k, v in by_kind.items()}
    report["on_topic_by_style"] = {
        style: summarize(
            [s for p, s in zip(pairs, scores, strict=True) if s is not None and p.style == style and p.kind == ON_TOPIC]
        )
        for style in sorted({p.style for p in pairs})
    }
    report["threshold_table"] = (
        [
            threshold_row(t, by_kind[ON_TOPIC], by_kind[OFF_TOPIC_CORPUS], by_kind[OFF_TOPIC_FOREIGN])
            for t in sorted({0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.7, current})
        ]
        if any(by_kind.values())
        else []
    )
    report["recommendation"] = recommend(
        by_kind[ON_TOPIC],
        by_kind[OFF_TOPIC_CORPUS],
        by_kind[OFF_TOPIC_FOREIGN],
        current=current,
        target_retention=args.target_retention,
        max_leakage=args.max_leakage,
        min_pairs=args.min_pairs,
        seed=args.seed,
    )
    stability = report.get("stability") or {}
    if stability.get("batch_independent") is False:
        report["recommendation"]["warning_batch_dependence"] = (
            f"a pair's score moved by {stability['max_abs_diff']} when scored alone. Scores depend on "
            "the other documents in the call, so an absolute floor does not transfer from this probe's "
            "~6-document calls to production's 40."
        )

    # ── Optional reference model on identical pairs ────────────────────────
    if args.reference_model_id and pairs and any(s is not None for s in scores):
        try:
            ref_scores, ref_section = score_pairs(reference_factory(args.reference_model_id), pairs)
        except Exception as exc:  # noqa: BLE001
            report["reference"] = {"model_id": args.reference_model_id, "error": _err(exc)}
        else:
            both = [(r, t) for r, t in zip(ref_scores, scores, strict=True) if r is not None and t is not None]
            report["reference"] = {
                "model_id": args.reference_model_id,
                "scoring": {k: v for k, v in ref_section.items() if k != "error"},
                "mapping": (
                    map_threshold(
                        [r for r, _ in both],
                        [t for _, t in both],
                        reference_threshold=(
                            args.reference_threshold if args.reference_threshold is not None else current
                        ),
                    )
                    if both
                    else {"error": ref_section.get("error") or {"type": "NoOverlap"}}
                ),
            }
    else:
        report["reference"] = {"skipped": "no --reference-model-id"}

    # ── Harvest: the reranker.py note's route ──────────────────────────────
    if args.harvest_since:
        since = datetime.fromisoformat(args.harvest_since)
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        loader = harvest_loader or (lambda **kw: asyncio.run(fetch_harvest(_dsn(), **kw)))
        try:
            harvested = loader(since=since, version=args.harvest_version)
        except Exception as exc:  # noqa: BLE001
            report["harvest"] = {"error": _err(exc)}
        else:
            report["harvest"] = analyze_harvest(
                harvested["rows"],
                versions=harvested["versions"],
                version=args.harvest_version,
                filtered_by_version=harvested["filtered_by_version"],
                current=current,
                since=since.isoformat(),
            )
    else:
        report["harvest"] = {"skipped": "no --harvest-since"}

    report["caveats"] = list(STATIC_CAVEATS)
    report["verdict"] = verdict(report)
    return report


def _dsn() -> str:
    from app.db.dsn import build_dsn  # noqa: PLC0415

    return build_dsn(scheme="postgresql", include_sslmode=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", choices=("corpus", "pairs", "none"), default="corpus")
    parser.add_argument("--pairs", type=Path, default=None, help="JSONL pairs file for --source pairs")
    parser.add_argument("--max-anchors", type=int, default=150)
    parser.add_argument("--negatives-per-anchor", type=int, default=4)
    parser.add_argument("--foreign-per-anchor", type=int, default=1)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--target-retention", type=float, default=0.95)
    parser.add_argument("--max-leakage", type=float, default=0.05)
    parser.add_argument("--min-pairs", type=int, default=50)
    parser.add_argument("--stability-samples", type=int, default=10)
    parser.add_argument("--current-threshold", type=float, default=None, help="override; default reads app.config")
    parser.add_argument(
        "--reference-model-id", default=None, help="a second Bedrock rerank model to score the same pairs"
    )
    parser.add_argument(
        "--reference-threshold",
        type=float,
        default=None,
        help="the reference model's threshold to map (default: the current value)",
    )
    parser.add_argument(
        "--harvest-since", default=None, help="ISO date: also harvest answer_runs since then (the reranker.py route)"
    )
    parser.add_argument("--harvest-version", default="cohere-bedrock:cohere.rerank-v3-5:0")
    parser.add_argument("--out", type=Path, default=Path("ops/validation/reports"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.source == "pairs" and not args.pairs:
        print("--source pairs needs --pairs FILE", file=sys.stderr)
        return 2

    report = run(args)

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = args.out / f"rerank_threshold_{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    print(f"\nreport written to {path}", file=sys.stderr)

    v = report["verdict"]
    if not v["verified_anything"]:
        print(
            f"\nPROBE FAILED: {v['summary']}\n"
            "Nothing was measured. Do not commit this report as evidence about the threshold.",
            file=sys.stderr,
        )
        return 1

    rec = report["recommendation"]
    print(
        f"\nRECOMMENDATION: status={rec.get('status')} current={rec.get('current')} "
        f"recommended={rec.get('recommended')} band={rec.get('band')} "
        f"auc={rec.get('auc_on_vs_off_corpus')}",
        file=sys.stderr,
    )
    print(
        "This changes nothing. RERANKER_SCORE_THRESHOLD_HOSTED is edited by a person, in app/config.py,\n"
        "citing this report. Read 'caveats' first: both biases push the band UP.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
