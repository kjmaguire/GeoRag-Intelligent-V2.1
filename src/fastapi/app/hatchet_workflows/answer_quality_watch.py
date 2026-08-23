"""Notice when answers get worse, without waiting for a support email.

THE GAP (OBS-12)
    Refusal rate, hallucination-guard fire rate, confidence and
    chunks-returned are all measured. Every one of them lives in an
    unscraped Prometheus registry: no dashboard, no alert, no nightly
    benchmark against the real backend. ``ops/runbooks/refusal-rate-
    spike.md`` is well written and its trigger condition is a Prometheus
    alert that has never existed here.

    So a reranker change, an embedding-dimension mismatch, or a Qdrant
    collection with degenerate payloads (the 2026-06-01 incident) makes
    retrieval return nothing useful, the guards do their job, the system
    starts refusing -- and the first person to notice is a user, a week
    later.

WHY THIS READS POSTGRES INSTEAD OF SHIPPING COUNTERS
    The audit's fix is to ship the four counters to Azure Monitor custom
    metrics and put a scheduled query rule on them. That works, and it is
    blocked on an Azure change nobody has made.

    It is also the weaker half of the answer, because the same facts are
    already persisted, durably and with history, on ``silver.answer_runs``:

        rejection_reason              refusal, and WHY
        hallucination_guard_results   NULL = chain did not run;
                                      {} = ran clean; anything else fired
        confidence                    the model's own number
        latency_ms
        answer_retrieval_items        what retrieval actually returned

    A counter tells you the rate right now. These rows tell you what
    changed and when, which is the question you actually have at 3am. No
    Azure change, no scrape target, no new dependency.

WHAT IT DOES NOT DO
    It does not measure whether answers are CORRECT. That needs the
    golden-question benchmark run against the live backend with a
    committed post-Cohere baseline, and neither exists yet (L423). This
    measures whether the system's own signals moved -- which is what
    catches "every answer has been wrong since Tuesday", and is not the
    same as knowing they are right.

THE SMALL-SAMPLE PROBLEM, WHICH IS THE REAL DESIGN CONSTRAINT
    This deployment serves almost no traffic. A day with three queries
    and one refusal is a 33% refusal rate and means nothing. Every
    comparison below is therefore gated on a minimum sample in BOTH
    windows, and a window that fails the gate reports
    ``insufficient_sample`` rather than a number -- because a watch that
    cries wolf on n=3 gets muted, and a muted watch is worse than none.
"""
from __future__ import annotations

import logging
from typing import Any

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.db.dsn import build_dsn
from app.hatchet_workflows import hatchet

log = logging.getLogger("georag.hatchet.answer_quality_watch")

#: Distinctive prefix for the one line an alert rule should match. Log
#: Analytics has no metric to threshold on, so the log line IS the signal
#: -- see deploy/azure/alerts/create-alerts.sh.
ALERT_MARKER = "ANSWER_QUALITY_REGRESSION"

#: Below this many runs in EITHER window, no comparison is made. Chosen
#: because it is the point at which a single refusal stops moving the rate
#: by more than the alert thresholds below: with 20 runs one refusal is
#: 5 points, which is under the 15-point trigger.
MIN_SAMPLE = 20

#: Absolute percentage-point rises that count as a regression. Absolute,
#: not relative: a refusal rate going 2% -> 6% is a 200% relative jump and
#: almost certainly noise at this traffic level, while 20% -> 35% is four
#: points of real users getting nothing.
REFUSAL_RISE_PP = 15.0
GUARD_FIRE_RISE_PP = 15.0
ZERO_EVIDENCE_RISE_PP = 15.0

#: Confidence is a 0-1 score, so this one is in score points.
CONFIDENCE_DROP = 0.15


WINDOW_SQL = """
WITH runs AS (
    SELECT
        ar.answer_run_id,
        ar.created_at,
        ar.rejection_reason,
        ar.hallucination_guard_results,
        ar.confidence,
        ar.latency_ms,
        (SELECT count(*) FROM silver.answer_retrieval_items ari
          WHERE ari.answer_run_id = ar.answer_run_id) AS evidence_count
    FROM silver.answer_runs ar
    WHERE ar.created_at >= NOW() - ($1::int || ' days')::interval
),
labelled AS (
    SELECT
        CASE
            WHEN created_at >= NOW() - INTERVAL '1 day' THEN 'current'
            ELSE 'baseline'
        END AS window,
        rejection_reason IS NOT NULL                         AS refused,
        (hallucination_guard_results IS NOT NULL
         AND hallucination_guard_results <> '{}'::jsonb)     AS guard_fired,
        confidence,
        latency_ms,
        evidence_count = 0                                   AS no_evidence
    FROM runs
)
SELECT
    window,
    count(*)::int                                            AS total_runs,
    count(*) FILTER (WHERE refused)::int                     AS refusals,
    count(*) FILTER (WHERE guard_fired)::int                 AS guard_fires,
    count(*) FILTER (WHERE no_evidence)::int                 AS zero_evidence,
    avg(confidence)::float                                   AS mean_confidence,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)::float
                                                             AS p95_latency_ms
FROM labelled
GROUP BY window
"""


class AnswerQualityWatchInput(BaseModel):
    """A cron sends no input, so every field must have a default."""

    baseline_days: int = Field(
        default=8,
        description=(
            "Total lookback. The most recent day is the CURRENT window and "
            "the rest is the baseline, so 8 compares yesterday against the "
            "seven days before it."
        ),
    )
    min_sample: int = Field(
        default=MIN_SAMPLE,
        description=(
            "Runs required in BOTH windows before any comparison is made. "
            "See the module docstring: this deployment's traffic is low "
            "enough that a rate over three queries is noise."
        ),
    )


class QualityWindow(BaseModel):
    total_runs: int = 0
    refusals: int = 0
    guard_fires: int = 0
    zero_evidence: int = 0
    mean_confidence: float | None = None
    p95_latency_ms: float | None = None

    @property
    def refusal_rate(self) -> float:
        return self.refusals / self.total_runs if self.total_runs else 0.0

    @property
    def guard_fire_rate(self) -> float:
        return self.guard_fires / self.total_runs if self.total_runs else 0.0

    @property
    def zero_evidence_rate(self) -> float:
        return self.zero_evidence / self.total_runs if self.total_runs else 0.0


class AnswerQualityWatchOutput(BaseModel):
    current: QualityWindow = Field(default_factory=QualityWindow)
    baseline: QualityWindow = Field(default_factory=QualityWindow)
    #: Empty when nothing moved. Each entry names the metric, both values
    #: and the threshold, so the log line is actionable without a query.
    regressions: list[str] = Field(default_factory=list)
    #: True when either window was too small to compare. NOT an error --
    #: the expected state on a quiet week.
    insufficient_sample: bool = False


def compare(
    current: QualityWindow,
    baseline: QualityWindow,
    *,
    min_sample: int = MIN_SAMPLE,
) -> tuple[list[str], bool]:
    """Return (regressions, insufficient_sample).

    Pure, so the judgement in here is testable without a database -- and
    the judgement is most of the value. Three things it deliberately does
    NOT do:

    * It does not alert on an IMPROVEMENT. A refusal rate falling is not
      an incident, even though it is an equally large change.
    * It does not alert on latency. Latency has its own signal and its own
      runbook; folding it in here would make a quality alert fire for an
      infrastructure problem.
    * It does not compound. Each metric is reported separately, because
      "three things moved" and "one thing moved three times" need
      different responses and a combined score cannot tell them apart.
    """
    if current.total_runs < min_sample or baseline.total_runs < min_sample:
        return [], True

    regressions: list[str] = []

    def rise(name: str, now: float, before: float, threshold_pp: float) -> None:
        delta_pp = (now - before) * 100.0
        if delta_pp >= threshold_pp:
            regressions.append(
                f"{name} rose {before:.1%} -> {now:.1%} "
                f"(+{delta_pp:.1f}pp, threshold +{threshold_pp:.0f}pp)"
            )

    rise("refusal_rate", current.refusal_rate, baseline.refusal_rate,
         REFUSAL_RISE_PP)
    rise("guard_fire_rate", current.guard_fire_rate, baseline.guard_fire_rate,
         GUARD_FIRE_RISE_PP)
    rise("zero_evidence_rate", current.zero_evidence_rate,
         baseline.zero_evidence_rate, ZERO_EVIDENCE_RISE_PP)

    # Confidence is the one that falls rather than rises. NULL means the
    # model reported none, which is not the same as reporting zero -- so a
    # window with no confidence at all is skipped rather than read as a
    # collapse to 0.0.
    if current.mean_confidence is not None and baseline.mean_confidence is not None:
        drop = baseline.mean_confidence - current.mean_confidence
        if drop >= CONFIDENCE_DROP:
            regressions.append(
                f"mean_confidence fell {baseline.mean_confidence:.3f} -> "
                f"{current.mean_confidence:.3f} "
                f"(-{drop:.3f}, threshold -{CONFIDENCE_DROP})"
            )

    return regressions, False


def parse_windows(rows: list[dict[str, Any]]) -> tuple[QualityWindow, QualityWindow]:
    """Split the grouped query result into (current, baseline).

    A window with no rows is absent from the result entirely, which is why
    this builds from defaults rather than indexing.
    """
    by_window = {str(row["window"]): row for row in rows}

    def build(name: str) -> QualityWindow:
        row = by_window.get(name)
        if row is None:
            return QualityWindow()
        return QualityWindow(
            total_runs=int(row["total_runs"] or 0),
            refusals=int(row["refusals"] or 0),
            guard_fires=int(row["guard_fires"] or 0),
            zero_evidence=int(row["zero_evidence"] or 0),
            mean_confidence=(
                float(row["mean_confidence"])
                if row["mean_confidence"] is not None else None
            ),
            p95_latency_ms=(
                float(row["p95_latency_ms"])
                if row["p95_latency_ms"] is not None else None
            ),
        )

    return build("current"), build("baseline")


answer_quality_watch = hatchet.workflow(
    name="answer_quality_watch",
    # 14:30 UTC, and the time is load-bearing.
    #
    # shutdown-sweep.sh scales hatchet-worker-cc to --min-replicas 0 and
    # stops the Flexible Server; startup-sweep.sh reverses it. The jobs
    # fire at 0 6,7 and 0 13,14 UTC and each drops the hour that is not
    # the right Pacific local time, so BOTH candidate hours are closed as
    # far as a schedule can know: nothing between 06:00 and 14:00 UTC
    # runs at all, because there is no worker to run it.
    #
    # This was written at 13:15 — inside the window every winter — which
    # is exactly the mistake tests/test_crons_avoid_the_shutdown_window.py
    # now catches.
    on_crons=["30 14 * * *"],
    input_validator=AnswerQualityWatchInput,
)


@answer_quality_watch.task(execution_timeout="10m", schedule_timeout="1h", retries=1)
async def watch(
    input: AnswerQualityWatchInput, ctx: Context,
) -> AnswerQualityWatchOutput:
    """Compare yesterday's answer signals against the trailing week.

    Deliberately NOT workspace-scoped. This is an operator-facing health
    check over the deployment, not a tenant-facing view, and splitting a
    low-traffic corpus by workspace would put every window under the
    minimum sample. It reads only aggregates -- counts and averages -- and
    no query text, answer text or user identity leaves this function.
    """
    conn = await asyncpg.connect(build_dsn(), statement_cache_size=0)
    try:
        rows = [
            dict(record)
            for record in await conn.fetch(WINDOW_SQL, input.baseline_days)
        ]
    finally:
        await conn.close()

    current, baseline = parse_windows(rows)
    regressions, insufficient = compare(
        current, baseline, min_sample=input.min_sample,
    )

    out = AnswerQualityWatchOutput(
        current=current,
        baseline=baseline,
        regressions=regressions,
        insufficient_sample=insufficient,
    )

    if insufficient:
        log.info(
            "answer_quality_watch: not enough traffic to compare "
            "(current=%d baseline=%d, need %d in both)",
            current.total_runs, baseline.total_runs, input.min_sample,
        )
    elif regressions:
        # One line, one marker, everything needed to act on it. This is
        # what the alert rule matches -- there is no metric to threshold.
        log.error(
            "%s: %s | current runs=%d refusals=%d guard_fires=%d "
            "zero_evidence=%d confidence=%s | baseline runs=%d",
            ALERT_MARKER,
            "; ".join(regressions),
            current.total_runs, current.refusals, current.guard_fires,
            current.zero_evidence,
            f"{current.mean_confidence:.3f}"
            if current.mean_confidence is not None else "n/a",
            baseline.total_runs,
        )
    else:
        log.info(
            "answer_quality_watch: steady (runs=%d refusal=%.1f%% "
            "guard_fire=%.1f%% zero_evidence=%.1f%%)",
            current.total_runs, current.refusal_rate * 100,
            current.guard_fire_rate * 100, current.zero_evidence_rate * 100,
        )

    return out
