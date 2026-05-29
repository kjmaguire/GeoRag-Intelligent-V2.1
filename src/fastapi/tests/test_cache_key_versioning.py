"""Cache key versioning tests — updated for v6 prefix (Module 10 Chunk 10.1, 2026-04-26).

History:
  v3 — DOCUMENT_SCOPE_VERSION (static int) in key hash
  v4 — live data_version values in key hash (Chunk 1, 2026-04-21)
  v5 — cache boundary moved to CachedRetrievalContext (Phase B addendum, 2026-04-21)
       GeoRAGResponse is never cached. Old v4 keys TTL out naturally.
  v6 — added _SYSTEM_PROMPT_VERSION slot (PV-02, Module 5 Phase B 2026-04-21)
       so prompt edits invalidate the cache without a RETRIEVAL_STRATEGY_VERSION bump.
       Old v5 keys are unreachable under the v6 prefix; they TTL out naturally.

10.1 drift fix: test previously asserted v5 prefix; orchestrator was correctly
bumped to v6 in Module 5 Phase B. Test updated to match current behaviour.
"""

from __future__ import annotations

from app.agent.orchestrator import _cache_key


def test_cache_key_prefix_is_v6():
    """Module 5 Phase B (PV-02) bumped the cache-key prefix from v5 to v6.

    v5 stored CachedRetrievalContext without prompt version — prompt edits
    could silently serve stale cache hits.
    v6 adds _SYSTEM_PROMPT_VERSION so any prompt change invalidates existing cache.
    Old v5 keys are unreachable under the v6 prefix; they TTL out naturally.

    10.1 drift fix: was asserting v5; code correctly uses v6.
    """
    key = _cache_key("how many holes?", "proj-1", {"spatial": True})
    assert key.startswith("georag:rag_cache:v6:"), (
        f"Expected v6 prefix, got: {key!r}"
    )


def test_cache_key_not_v4():
    """v4 prefix must not appear — it was the answer-level cache (spec violation)."""
    key = _cache_key("how many holes?", "proj-1", {"spatial": True})
    assert "v4" not in key, f"Stale v4 prefix found in key: {key!r}"


def test_cache_key_differs_when_workspace_data_version_changes():
    """Bumping workspace_data_version must invalidate previously-cached keys."""
    q = "how many holes?"
    pid = "proj-1"
    cats = {"spatial": True}

    key_v5 = _cache_key(q, pid, cats, workspace_data_version=5)
    key_v6 = _cache_key(q, pid, cats, workspace_data_version=6)

    assert key_v5 != key_v6, "bumping workspace_data_version must change the cache key"


def test_cache_key_stable_for_identical_inputs():
    """Same inputs + same version produce the same key."""
    cats = {"spatial": True, "documents": True}
    k1 = _cache_key("test query", "proj-abc", cats)
    k2 = _cache_key("test query", "proj-abc", cats)
    assert k1 == k2
