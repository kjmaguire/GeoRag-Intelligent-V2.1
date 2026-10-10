"""train_source_trust counts the citations that point into each report.

It joined ``answer_citation_items.evidence_id`` to ``reports.report_id`` -- an
evidence_items id against a report id -- so the join never matched. Every source
had zero citations and was skipped as "low signal" (or, with
``min_citations_per_source=0``, scored on a neutral 0.5 citation rate), whatever
the workspace had actually cited. The workflow ran green and trained nothing.

A citation names a passage (the only writer fills ``passage_id``), a passage names
its document, and for a report that document id is ``reports.report_id``. Fixing
the join also exposed ``_recency_factor`` treating the DATE column as a datetime.

Real Postgres: the claim is about a join, which a scripted connection cannot
check. Runs in the integration manifest job.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import date
from uuid import uuid4

import asyncpg
import pytest

from app.hatchet_workflows.train_source_trust import (
    TrainSourceTrustInput,
    _compute_trust,
)
from app.hatchet_workflows.train_source_trust import (
    execute as train_source_trust_execute,
)

pytestmark = pytest.mark.integration

DEFAULT_WORKSPACE = "a0000000-0000-0000-0000-000000000001"
FILED = date(2024, 1, 15)


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@dataclass
class _Seed:
    report_cited: str       # 4 citations, 3 of them valid
    report_thin: str        # 1 citation
    report_uncited: str     # none
    summary_passage: str    # a passage with no document (ADR-0012 summary), cited once
    run_id: str
    model_version: str


async def _passage(conn: asyncpg.Connection, document_id: str | None, label: str) -> str:
    return await conn.fetchval(
        "INSERT INTO silver.document_passages "
        "  (document_id, workspace_id, revision_number, text, text_hash, ordinal) "
        "VALUES ($1::uuid, $2::uuid, 1, $3, $4, 0) RETURNING passage_id::text",
        document_id, DEFAULT_WORKSPACE, f"passage {label}",
        hashlib.sha256(f"{label}-{uuid4()}".encode()).hexdigest(),
    )


async def _report(conn: asyncpg.Connection, title: str, filed: date | None, parser: str | None) -> str:
    return await conn.fetchval(
        "INSERT INTO silver.reports (report_id, title, filing_date, parser_used, workspace_id) "
        "VALUES (gen_random_uuid(), $1, $2, $3, $4::uuid) RETURNING report_id::text",
        title, filed, parser, DEFAULT_WORKSPACE,
    )


async def _cite(
    conn: asyncpg.Connection, run_id: str, passage_id: str, n: int, rejected: bool = False,
) -> None:
    await conn.execute(
        "INSERT INTO silver.answer_citation_items "
        "  (answer_run_id, workspace_id, passage_id, marker_text, rejection_reason) "
        "VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5)",
        run_id, DEFAULT_WORKSPACE, passage_id, f"[DATA:{n}]",
        "fabricated_number" if rejected else None,
    )


@pytest.fixture
async def conn():
    c = await asyncpg.connect(_dsn(), statement_cache_size=0)
    await c.execute(
        "SELECT set_config('app.workspace_id', $1, false)", DEFAULT_WORKSPACE,
    )
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
async def seeded(conn):
    tag = uuid4().hex[:8]
    cited = await _report(conn, f"join test cited {tag}", FILED, "pdfminer.six")
    thin = await _report(conn, f"join test thin {tag}", None, None)
    uncited = await _report(conn, f"join test uncited {tag}", None, None)
    run_id = await conn.fetchval(
        "INSERT INTO silver.answer_runs "
        "  (workspace_id, query_text, query_class, workspace_data_version_at_query) "
        "VALUES ($1::uuid, $2, 'factual', 1) RETURNING answer_run_id::text",
        DEFAULT_WORKSPACE, f"train_source_trust join test {tag}",
    )
    a1 = await _passage(conn, cited, "a1")
    a2 = await _passage(conn, cited, "a2")
    b1 = await _passage(conn, thin, "b1")
    summary = await _passage(conn, None, "summary")
    await _cite(conn, run_id, a1, 1)
    await _cite(conn, run_id, a1, 2)
    await _cite(conn, run_id, a1, 3, rejected=True)
    await _cite(conn, run_id, a2, 4)
    await _cite(conn, run_id, b1, 5)
    await _cite(conn, run_id, summary, 6)
    seed = _Seed(cited, thin, uncited, summary, run_id, f"t16_{tag}")
    try:
        yield seed
    finally:
        await conn.execute(
            "DELETE FROM silver.source_trust_scores WHERE model_version = $1",
            seed.model_version,
        )
        await conn.execute("DELETE FROM silver.answer_runs WHERE answer_run_id = $1::uuid", run_id)
        await conn.execute(
            "DELETE FROM silver.reports WHERE report_id = ANY($1::uuid[])",
            [cited, thin, uncited],
        )
        await conn.execute(
            "DELETE FROM silver.document_passages WHERE passage_id = $1::uuid", summary,
        )


async def _train(seed: _Seed, *, min_citations: int):
    return await train_source_trust_execute.aio_mock_run(
        TrainSourceTrustInput(
            workspace_id=DEFAULT_WORKSPACE,
            initiated_by_user_id=1,
            min_citations_per_source=min_citations,
            model_version=seed.model_version,
        ),
    )


async def _scores(conn: asyncpg.Connection, seed: _Seed) -> dict[str, float]:
    rows = await conn.fetch(
        "SELECT source_document_id::text AS sid, trust_score "
        "  FROM silver.source_trust_scores WHERE model_version = $1",
        seed.model_version,
    )
    return {r["sid"]: float(r["trust_score"]) for r in rows}


@pytest.mark.asyncio
async def test_a_report_is_scored_on_the_citations_that_point_into_it(conn, seeded) -> None:
    out = await _train(seeded, min_citations=2)

    assert out.success is True
    scores = await _scores(conn, seeded)

    assert seeded.report_cited in scores, "the cited report was skipped as low signal"
    # 4 citations into the report, 3 of them not rejected, doctype from pdfminer.
    expected, _ = _compute_trust(
        citations_total=4, citations_validated=3, filing_date=FILED, doctype="ni_43_101",
    )
    assert scores[seeded.report_cited] == pytest.approx(expected, abs=1e-3)
    # One citation is under the threshold of two, and a report nobody cited is
    # not scored at all; the document-less summary passage is nobody's source.
    assert seeded.report_thin not in scores
    assert seeded.report_uncited not in scores
    assert seeded.summary_passage not in scores


@pytest.mark.asyncio
async def test_the_citation_rate_is_what_separates_two_scores(conn, seeded) -> None:
    """With no minimum, the uncited report is scored on the neutral rate and the
    cited one on its real rate (3 of 4); before the fix both came out neutral."""
    out = await _train(seeded, min_citations=0)

    assert out.success is True
    scores = await _scores(conn, seeded)
    neutral, _ = _compute_trust(
        citations_total=0, citations_validated=0, filing_date=None, doctype="company_internal",
    )
    thin_expected, _ = _compute_trust(
        citations_total=1, citations_validated=1, filing_date=None, doctype="company_internal",
    )
    cited_expected, _ = _compute_trust(
        citations_total=4, citations_validated=3, filing_date=FILED, doctype="ni_43_101",
    )
    assert scores[seeded.report_uncited] == pytest.approx(neutral, abs=1e-3)
    assert scores[seeded.report_thin] == pytest.approx(thin_expected, abs=1e-3)
    assert scores[seeded.report_cited] == pytest.approx(cited_expected, abs=1e-3)
