"""Ticket Triage Agent (§10.9 / §25.4).

Categorizes incoming ``ops.support_tickets`` rows. Phase G.5 minimum-
viable body: extracts severity and category signals from the ticket's
description text using keyword heuristics + the ticket's current
severity/category fields, then suggests adjustments.

Doc-phase 98 skeleton → Phase G.5 graduation.
"""
from __future__ import annotations
from app.agent.workspace_context import LEGACY_DEFAULT_TENANT_UUID

import os
import re
from typing import Any
from uuid import UUID

import asyncpg

from app.agents import AgentContext, georag_agent


# Severity hint keywords — descending order of severity.
_SEVERITY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("critical", [
        r"\bdown\b", r"outage", r"complete failure", r"can'?t access",
        r"data loss", r"security breach", r"production stopped",
        r"all users", r"emergency",
    ]),
    ("high", [
        r"\bcrash(ed|ing)?\b", r"slow", r"timeout", r"error\b",
        r"broken", r"missing data", r"wrong results", r"hallucinat",
    ]),
    ("medium", [
        r"\bbug\b", r"unexpected", r"confused", r"\bweird\b",
        r"intermittent", r"sometimes fails",
    ]),
    ("low", [
        r"feature request", r"would be nice", r"cosmetic",
        r"typo", r"unclear documentation", r"\benhancement\b",
    ]),
]

# Category hint keywords. ops.support_tickets.category accepts:
# wrong_answer | failed_ingestion | failed_report | integration_issue |
# performance | other
_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "wrong_answer": [
        "hallucin", "wrong answer", "incorrect citation", "missed citation",
        "no answer", "refused", "model said", "rag", "wrong data",
        "missing data",
    ],
    "failed_ingestion": [
        "ingest", "ocr", "pdf failed", "drill log", "assay missing",
        "import error", "upload failed", "couldn't read",
    ],
    "failed_report": [
        "report failed", "export failed", "pdf generation", "docx",
        "couldn't generate", "report missing",
    ],
    "integration_issue": [
        "sharepoint", "onedrive", "teams", "slack", "sso", "auth",
        "api", "webhook", "kestra", "activepieces",
    ],
    "performance": [
        "slow", "timeout", "hang", "spinner", "laggy", "freeze",
    ],
}


def _suggest_severity(text: str) -> tuple[str, list[str]]:
    """Walk severity buckets top-down; return (suggested, matched_terms)."""
    lower = text.lower()
    matched: list[str] = []
    for severity, patterns in _SEVERITY_KEYWORDS:
        for pat in patterns:
            if re.search(pat, lower):
                matched.append(pat.strip("\\b"))
                return severity, matched
    return "medium", matched


def _suggest_category(text: str) -> tuple[str, list[str]]:
    """Pick the category with the most keyword hits in the description."""
    lower = text.lower()
    hits_per_category: dict[str, list[str]] = {}
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        matched = [k for k in keywords if k in lower]
        if matched:
            hits_per_category[cat] = matched
    if not hits_per_category:
        return "other", []
    best = max(hits_per_category.items(), key=lambda kv: len(kv[1]))
    return best[0], best[1]


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@georag_agent(
    name="Ticket Triage Agent",
    risk_tier="R1",
    version="0.2.0",
)
async def ticket_triage(
    ctx: AgentContext,
    *,
    ticket_id: UUID | str,
) -> dict[str, Any]:
    """Suggest category + severity based on the ticket's description.

    Phase G.5 minimum-viable body — deterministic keyword heuristics.
    Returns a dict the Support Cockpit can render directly:

        {
            "ticket_id": "<uuid>",
            "current_severity": "<str>",
            "current_category": "<str>",
            "suggested_severity": "<str>",
            "suggested_category": "<str>",
            "severity_evidence": [<matched terms>],
            "category_evidence": [<matched keywords>],
            "should_change": bool,
        }
    """
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        # Block-3 RLS: ops.support_tickets is workspace_id-scoped. The
        # agent reads from whatever workspace the caller's context
        # carries; fall back to the Default Workspace (ticket is in
        # the default scope per the support fixtures).
        ws = str(ctx.workspace_id) if ctx and ctx.workspace_id \
             else LEGACY_DEFAULT_TENANT_UUID
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", ws,
        )
        row = await conn.fetchrow(
            """
            SELECT ticket_id::text AS id, severity, category, description
              FROM ops.support_tickets
             WHERE ticket_id = $1::uuid
            """,
            str(ticket_id),
        )
    finally:
        await conn.close()

    if row is None:
        return {
            "ticket_id": str(ticket_id),
            "error": "ticket not found",
        }

    text = row["description"] or ""
    suggested_severity, severity_evidence = _suggest_severity(text)
    suggested_category, category_evidence = _suggest_category(text)

    return {
        "ticket_id": row["id"],
        "current_severity": row["severity"],
        "current_category": row["category"],
        "suggested_severity": suggested_severity,
        "suggested_category": suggested_category,
        "severity_evidence": severity_evidence,
        "category_evidence": category_evidence,
        "should_change": (
            suggested_severity != row["severity"]
            or suggested_category != row["category"]
        ),
    }
