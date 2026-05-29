"""Phase 13 Step 1 — migrated from ``app.agent.llm_classifier`` inline
``_CLASSIFIER_SYSTEM_PROMPT``. Second prompt to land under the canonical
``app/agent/prompts/`` tree after Phase 12 Step 2's ``rephrase_system``.

Consumer: ``app.agent.llm_classifier`` — query-routing classifier used
when the keyword classifier can't decide which tool bucket a question
falls into.

Output contract: strict JSON with all seven boolean keys present. The
consumer's ``_parse_classifier_json`` tolerates code-fence wrappers and
missing keys (treats them as False); empty/garbage responses fall back
to all-False.

Version bookkeeping: bump ``PROMPT_VERSION`` when ``SYSTEM_PROMPT``
changes materially. The pre-commit hook
``system-prompt-version-bump`` enforces the bump if both this file and
``orchestrator.py``'s ``_SYSTEM_PROMPT_VERSION`` appear in the same diff.
"""

from __future__ import annotations

PROMPT_VERSION = "0.1.0"

SYSTEM_PROMPT = """\
You are a query-routing classifier for a geological RAG system. The user \
asked a question that a keyword classifier couldn't route. Your job is to \
categorise it into one or more of the available tool buckets.

Buckets (each is boolean):
  spatial           — asks about drill hole locations, geometry, or counts
  documents         — asks about NI 43-101 reports, published literature
  graph             — names a specific deposit, formation, company, or QP
  assay             — asks about grades, element concentrations, samples
  downhole          — asks about lithology or intervals along a specific hole
  targeting         — asks where to drill next / best drill target
  public_geo — asks about government-published mineral records

Rules:
1. A query can match multiple buckets. Set each to true or false.
2. Err toward TRUE — a broader net is cheaper than a missed retrieval.
3. Output only valid JSON, nothing else:
{"spatial":true,"documents":true,"graph":false,"assay":false,"downhole":false,"targeting":false,"public_geo":false}
"""


__all__ = ["PROMPT_VERSION", "SYSTEM_PROMPT"]
