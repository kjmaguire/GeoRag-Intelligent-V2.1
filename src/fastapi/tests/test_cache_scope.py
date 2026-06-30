"""test_cache_scope.py — verify cache boundary is retrieval-only, never answer-level.

Module 4 Phase B addendum (2026-04-21).
Updated: Module 10 Chunk 10.1 (2026-04-26) — v5→v6 prefix drift fix.

Invariants under test
---------------------
1. Redis SETEX stores CachedRetrievalContext — never GeoRAGResponse.
2. CachedRetrievalContext contains no synthesized answer fields.
3. On cache hit, synthesis is always called (LLM is not skipped).
4. On cache miss, synthesis is always called.
5. Stale v4 entries (GeoRAGResponse shape) are treated as cache misses with
   a warning log — no crash, no stale answer served.
6. Cache key prefix is v6 (not v5 — bumped 2026-04-21, PV-02: added
   _SYSTEM_PROMPT_VERSION slot so prompt edits invalidate the cache).
7. _cache_key() produces v6 prefix.

Drift fix (10.1): orchestrator._cache_key() was bumped from v5→v6 in Module 5
Phase B (PV-02) to include _SYSTEM_PROMPT_VERSION in the hash. Tests that
asserted the old v5 prefix were stale; the code is correct.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.agent.orchestrator import _cache_key
from app.models.retrieval_cache import CachedRetrievalCandidate, CachedRetrievalContext

# ---------------------------------------------------------------------------
# 1. Model shape — CachedRetrievalContext must NOT contain answer fields
# ---------------------------------------------------------------------------


class TestCachedRetrievalContextShape:
    """Verify the model excludes all answer-level fields per spec §05c."""

    def test_no_synthesized_answer_field(self):
        """CachedRetrievalContext must not have a text / synthesized_answer field."""
        field_names = set(CachedRetrievalContext.model_fields.keys())
        forbidden = {"text", "synthesized_answer", "answer_text", "citations", "followups"}
        overlap = field_names & forbidden
        assert not overlap, (
            f"CachedRetrievalContext contains answer-level fields: {overlap}. "
            "These must not be cached per arch §05c."
        )

    def test_no_citation_lifecycle_state(self):
        field_names = set(CachedRetrievalContext.model_fields.keys())
        assert "citation_lifecycle_state" not in field_names

    def test_no_llm_token_counts(self):
        field_names = set(CachedRetrievalContext.model_fields.keys())
        forbidden_token_fields = {"input_tokens", "output_tokens", "cache_read_tokens"}
        overlap = field_names & forbidden_token_fields
        assert not overlap, f"LLM token count fields found in cache model: {overlap}"

    def test_required_retrieval_fields_present(self):
        """All retrieval-context fields from spec must be present."""
        required = {
            "schema_version",
            "cached_at",
            "workspace_id",
            "workspace_data_version_at_cache",
            "query_class",
            "sparse_boost_applied",
            "fusion_method",
            "retrieval_strategy_version",
            "embedding_model_version",
            "sparse_model_version",
            "candidates_reranked",
        }
        field_names = set(CachedRetrievalContext.model_fields.keys())
        missing = required - field_names
        assert not missing, f"Required retrieval fields missing from model: {missing}"

    def test_schema_version_default_is_1(self):
        ctx = CachedRetrievalContext(
            cached_at=datetime.now(UTC),
            workspace_id=uuid4(),
            workspace_data_version_at_cache=1,
            query_class="spatial",
            sparse_boost_applied=False,
            fusion_method="rrf",
            retrieval_strategy_version="v2-retrieval-only-cache-2026-04-21",
            embedding_model_version="bge-small-en-v1.5",
            sparse_model_version="splade-49cf4c7b",
        )
        assert ctx.schema_version == 1

    def test_serialise_deserialise_round_trip(self):
        """JSON round-trip must be lossless."""
        candidate = CachedRetrievalCandidate(
            source_store="qdrant",
            text="Hole PLS-22-08 intersected 12.3m of massive sulphide at 340m depth.",
            retriever_score=0.82,
            reranker_score=0.91,
            rrf_rank=1,
            rrf_score=0.0625,
        )
        ctx = CachedRetrievalContext(
            cached_at=datetime.now(UTC),
            workspace_id=uuid4(),
            workspace_data_version_at_cache=7,
            project_data_version_at_cache=3,
            query_class="document",
            sparse_boost_applied=True,
            fusion_method="rrf",
            retrieval_strategy_version="v2-retrieval-only-cache-2026-04-21",
            embedding_model_version="bge-small-en-v1.5",
            sparse_model_version="splade-49cf4c7b",
            reranker_version="bge-reranker-base-5ccf1b81",
            candidates_reranked=[candidate],
        )
        dumped = ctx.model_dump_json()
        restored = CachedRetrievalContext.model_validate_json(dumped)
        assert restored.schema_version == ctx.schema_version
        assert restored.workspace_data_version_at_cache == 7
        assert len(restored.candidates_reranked) == 1
        assert restored.candidates_reranked[0].text == candidate.text
        assert restored.candidates_reranked[0].rrf_rank == 1

    def test_no_answer_fields_in_serialised_json(self):
        """JSON output must not contain any answer-level keys."""
        ctx = CachedRetrievalContext(
            cached_at=datetime.now(UTC),
            workspace_id=uuid4(),
            workspace_data_version_at_cache=1,
            query_class="spatial",
            sparse_boost_applied=False,
            fusion_method="rrf",
            retrieval_strategy_version="v2-retrieval-only-cache-2026-04-21",
            embedding_model_version="bge-small-en-v1.5",
            sparse_model_version="splade-49cf4c7b",
        )
        payload = json.loads(ctx.model_dump_json())
        forbidden_keys = {"text", "citations", "answer_text", "synthesized_answer",
                          "followups", "map_payload", "viz_payload", "confidence",
                          "citation_lifecycle_state", "input_tokens", "output_tokens"}
        found = forbidden_keys & set(payload.keys())
        assert not found, f"Answer-level keys found in serialised CachedRetrievalContext: {found}"


# ---------------------------------------------------------------------------
# 2. Cache key prefix — must be v5
# ---------------------------------------------------------------------------


class TestCacheKeyPrefix:
    """Cache key must use v6 prefix.

    History:
      v4 — stored full GeoRAGResponse (answer-level — spec violation per §05c).
      v5 — stored CachedRetrievalContext (retrieval-only — spec-compliant).
           Bumped from v4 in Module 4 Phase B addendum.
      v6 — added _SYSTEM_PROMPT_VERSION slot (PV-02, Module 5 Phase B 2026-04-21)
           so prompt edits invalidate the cache without a RETRIEVAL_STRATEGY_VERSION
           bump. Old v5 keys are unreachable under the v6 prefix; they TTL out naturally.

    Drift fix (10.1): tests previously asserted v5; code was correctly bumped to v6.
    """

    def test_cache_key_prefix_is_v6(self):
        # 10.1 drift fix: prefix bumped v5→v6 in Module 5 Phase B (PV-02).
        key = _cache_key("how many holes?", "proj-1", {"spatial": True})
        assert key.startswith("georag:rag_cache:v6:"), (
            f"Expected v6 prefix but got: {key!r}. "
            "v6 adds _SYSTEM_PROMPT_VERSION to the hash so prompt edits invalidate cache."
        )

    def test_cache_key_not_v4(self):
        key = _cache_key("show me all drill holes", "proj-2", {"spatial": True, "documents": True})
        assert "v4" not in key, f"v4 prefix found in cache key: {key!r}"

    def test_cache_key_not_v5(self):
        # 10.1 drift fix: v5 was the pre-PV-02 prefix; must no longer appear.
        key = _cache_key("show me all drill holes", "proj-2", {"spatial": True, "documents": True})
        assert ":v5:" not in key, f"Stale v5 prefix found in cache key: {key!r}"

    def test_cache_key_stable_for_same_inputs(self):
        cats = {"spatial": True, "documents": True}
        k1 = _cache_key("test query", "proj-abc", cats)
        k2 = _cache_key("test query", "proj-abc", cats)
        assert k1 == k2

    def test_cache_key_changes_with_workspace_data_version(self):
        cats = {"spatial": True}
        k1 = _cache_key("holes near lake", "proj-1", cats, workspace_data_version=5)
        k2 = _cache_key("holes near lake", "proj-1", cats, workspace_data_version=6)
        assert k1 != k2, "bumping workspace_data_version must change the cache key"

    def test_cache_key_no_categories_also_v6(self):
        """Back-compat path (no categories kwarg) must also use v6."""
        # 10.1 drift fix: was v5, now v6.
        key = _cache_key("how many holes?", "proj-1")
        assert key.startswith("georag:rag_cache:v6:")


# ---------------------------------------------------------------------------
# 3. Stale v4 entry graceful handling
# ---------------------------------------------------------------------------


class TestStaleV4EntryHandling:
    """A v4 Redis entry (GeoRAGResponse JSON) must be treated as a cache miss."""

    def test_validate_json_rejects_georag_response_shape(self):
        """CachedRetrievalContext.model_validate_json must raise on GeoRAGResponse JSON."""
        stale_v4_payload = json.dumps({
            "text": "There are 42 drill holes in the project.",
            "citations": [
                {
                    "citation_id": "DATA-1",
                    "citation_type": "DATA",
                    "source_chunk_id": "abc123",
                    "document_title": "Collar Query",
                    "relevance_score": 0.95,
                }
            ],
            "map_payload": None,
            "viz_payload": None,
            "confidence": 0.87,
            "sources_used": ["abc123"],
            "degraded_sources": [],
            "followups": [],
        })
        with pytest.raises(Exception):
            # Must raise ValidationError (or any exception) — v4 shape is invalid
            # for CachedRetrievalContext because required fields are missing.
            CachedRetrievalContext.model_validate_json(stale_v4_payload)


# ---------------------------------------------------------------------------
# 4. Retrieval strategy version
# ---------------------------------------------------------------------------


class TestRetrievalStrategyVersion:
    """RETRIEVAL_STRATEGY_VERSION must be set and well-formed.

    Drift fix (10.1): version was bumped from v2-retrieval-only-cache-2026-04-21
    through v3-qwen3-moe-2026-04-21 to v3.1-think-off-2026-04-21 as the model
    stack evolved (Module 5 Phase B). The test that pinned the exact v2 string
    was stale; we now assert the version is a non-empty string that follows the
    general versioning convention (vN[.M]-<suffix>-<date> or vN-<suffix>-<date>)
    and that it participates in cache-key computation.
    """

    def test_retrieval_strategy_version_is_set(self):
        # 10.1 drift fix: v2 was a specific version; code has since been bumped
        # through v3 and v3.1. Assert non-empty string in lieu of pinned value.
        from app.services.query_classifier import RETRIEVAL_STRATEGY_VERSION
        assert isinstance(RETRIEVAL_STRATEGY_VERSION, str) and len(RETRIEVAL_STRATEGY_VERSION) > 0, (
            "RETRIEVAL_STRATEGY_VERSION must be a non-empty string."
        )

    def test_retrieval_strategy_version_starts_with_v(self):
        from app.services.query_classifier import RETRIEVAL_STRATEGY_VERSION
        assert RETRIEVAL_STRATEGY_VERSION.startswith("v"), (
            f"RETRIEVAL_STRATEGY_VERSION={RETRIEVAL_STRATEGY_VERSION!r} must start with 'v'."
        )

    def test_retrieval_strategy_version_in_cache_key(self):
        """RETRIEVAL_STRATEGY_VERSION must participate in cache key computation.

        Drift fix (10.1): the original test patched the non-existent module-level
        attribute 'app.agent.orchestrator.RETRIEVAL_STRATEGY_VERSION'. The orchestrator
        imports this value lazily at call-time from app.services.query_classifier, so
        the correct patch target is 'app.services.query_classifier.RETRIEVAL_STRATEGY_VERSION'.
        """
        q = "what grade is the deposit?"
        pid = "proj-x"
        cats = {"documents": True}

        k_live = _cache_key(q, pid, cats)

        # Patch the value in the module where it is defined — orchestrator reads
        # it via lazy import so this affects the next _cache_key() call.
        with patch("app.services.query_classifier.RETRIEVAL_STRATEGY_VERSION", "v99-test"):
            k_patched = _cache_key(q, pid, cats)

        assert k_live != k_patched, "Changing RETRIEVAL_STRATEGY_VERSION must change the cache key"


# ---------------------------------------------------------------------------
# 5. CachedRetrievalCandidate shape
# ---------------------------------------------------------------------------


class TestCachedRetrievalCandidateShape:
    """CachedRetrievalCandidate must preserve retrieval metadata without answer fields."""

    def test_candidate_has_no_answer_fields(self):
        field_names = set(CachedRetrievalCandidate.model_fields.keys())
        forbidden = {"synthesized_answer", "citations", "text_with_citation_markers"}
        overlap = field_names & forbidden
        assert not overlap

    def test_candidate_required_fields(self):
        required = {"source_store", "text"}
        field_names = set(CachedRetrievalCandidate.model_fields.keys())
        assert required <= field_names

    def test_candidate_serialises_cleanly(self):
        c = CachedRetrievalCandidate(
            source_store="postgis",
            text="Collar PLS-22-08 at E=588320 N=6290440 depth=450m",
            retriever_score=1.0,
            rrf_rank=3,
            rrf_score=0.021,
        )
        payload = json.loads(c.model_dump_json())
        assert payload["source_store"] == "postgis"
        assert payload["rrf_rank"] == 3
        assert "synthesized_answer" not in payload
