"""Phase 12 Step 2 — migrated from ``app.agent.escalation`` inline
``_REPHRASE_SYSTEM_PROMPT`` (first prompt to land under
``app/agent/prompts/`` after the Phase 11 Step 3 bootstrap).

Consumer: ``app.agent.escalation`` — bounded escalation path when the
keyword classifier falls through to empty results (R9). The LLM is
asked to propose alternative phrasings of the user's question; the
deterministic tool dispatch then retries against each phrasing.

Output contract: the consumer expects strict JSON of shape
``{"rephrasings": ["alt 1", "alt 2", "alt 3"]}``. The prompt enforces
this format explicitly; the consumer's ``_parse_rephrasings_json``
helper is tolerant of code-fence wrappers + leading chatter from
smaller models that ignore the "nothing else" instruction.

Version bookkeeping: bump ``PROMPT_VERSION`` when ``SYSTEM_PROMPT``
changes materially. The pre-commit hook
``system-prompt-version-bump`` (Phase 5 Step 3 → Phase 11 Step 5 fully
active) enforces the bump if both this file and ``orchestrator.py``'s
``_SYSTEM_PROMPT_VERSION`` appear in the same diff.
"""

from __future__ import annotations

PROMPT_VERSION = "0.1.0"

SYSTEM_PROMPT = """You are a query rewriting tool for a geological \
RAG system. You propose alternative phrasings of a user's question to help a \
keyword-based classifier find relevant data.

Rules:
1. Keep each alternative under 25 words.
2. Use different keywords than the original query where possible.
3. Focus on geological/exploration terminology that appears in NI 43-101 \
reports, drill-log databases, or ArcGIS FeatureServers.
4. Do NOT invent data, hole IDs, or specific numbers.
5. Output exactly in this JSON format, nothing else:
{"rephrasings": ["alt 1", "alt 2", "alt 3"]}
"""


__all__ = ["PROMPT_VERSION", "SYSTEM_PROMPT"]
