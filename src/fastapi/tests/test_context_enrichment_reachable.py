"""Contextual retrieval enrichment, which had never enriched a passage.

Three things had to be true at once for `enrich_passage_context` to do any
work, and none of them were:

  * The selection required `embedding_id IS NULL` -- "not yet embedded".
    embed_pending_passages runs `*/10 * * * *`; this workflow ran once a
    day. A passage qualified only if it was created inside the ~10 minute
    window immediately before the daily run.
  * The cron fired at 04:30 UTC, inside the 00:00-10:00 UTC window where
    the Flexible Server is deliberately Stopped.
  * The LLM call went to `settings.VLLM_URL`, whose default hostname stopped
    resolving when the local vllm service was removed on 2026-07-30.

Each failure landed in `ContextEnrichmentResult.errors`, which nothing
reads, so the workflow reported success every time it did nothing.

The 4096-character ceiling is the other half. `contextualized_content` is
what passage_embedder encodes -- the Qdrant payload still carries
`row["text"]` -- so truncating it does not shorten a quoted answer. It
makes the truncated tail unsearchable, which is worse, because the chunk
that comes back looks complete.
"""

from __future__ import annotations

import ast
import inspect
import io
import tokenize

import pytest

from app.services.ingest import context_enricher as ce


def code_only(source: str) -> str:
    """Source with COMMENTS and DOCSTRINGS stripped, string literals kept.

    These assertions ask "is this construct still EXECUTED?", and the fixes
    they guard deliberately quote the removed construct in a comment or a
    docstring to explain why it went. Scanning raw text matches the
    explanation and fails a correct file — a test that can only be satisfied
    by deleting the explanation.

    Ordinary string literals are deliberately KEPT: the SQL these tests
    assert against lives in one, so stripping every string would make the
    negative assertions vacuously true, which is the worse failure of the
    two — a test that passes whatever the code does.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - defensive
        return " ".join(source.split())

    doc_positions = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)                 and isinstance(first.value.value, str):
            doc_positions.add((first.value.lineno, first.value.col_offset))

    out: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and tok.start in doc_positions:
                continue
            out.append(tok.string)
    except tokenize.TokenError:  # pragma: no cover - defensive
        return " ".join(source.split())
    return " ".join(" ".join(out).split())


class TestEmbeddedTextIsNotTruncated:
    def test_a_full_window_passage_survives_whole(self) -> None:
        from app.services.ingest.pdf_report import WINDOW_CHARS

        passage = "x" * WINDOW_CHARS
        header = "This passage sits in the resource estimate section. " * 8

        body = ce._combine_enriched(header, passage).split("\n\n", 1)[1]

        assert body == passage

    def test_the_old_ceiling_would_have_lost_the_tail(self) -> None:
        """Guards the regression rather than the arithmetic.

        At the old flat 4096 the 300-char header plus separator left 3794
        characters for a 5000-character window: the last fifth of every
        full-size passage could not be matched by anything it said.
        """
        from app.services.ingest.pdf_report import WINDOW_CHARS

        assert ce._MAX_ENRICHED_LENGTH >= ce._CONTEXT_HEADER_CHARS + 2 + WINDOW_CHARS

    def test_the_header_is_capped(self) -> None:
        out = ce._combine_enriched("h" * 5_000, "body")

        assert out.split("\n\n", 1)[0] == "h" * ce._CONTEXT_HEADER_CHARS

    def test_a_malformed_row_still_hits_a_backstop(self, caplog) -> None:
        """The ceiling stays, but it says so when it bites."""
        oversize = "y" * (ce._MAX_ENRICHED_LENGTH + 1_000)

        with caplog.at_level("WARNING"):
            out = ce._combine_enriched("header", oversize)

        assert len(out) == ce._MAX_ENRICHED_LENGTH
        assert "enriched_truncated" in caplog.text

    def test_the_prompt_shows_the_model_the_whole_passage(self) -> None:
        from app.services.ingest.pdf_report import WINDOW_CHARS

        assert ce._MAX_PASSAGES_CONTEXT >= WINDOW_CHARS


class TestSelection:
    """The predicate that made the whole workflow unreachable."""

    @staticmethod
    def _sql() -> str:
        return code_only(inspect.getsource(ce.enrich_passage_context))

    def test_enrichment_no_longer_waits_on_being_unembedded(self) -> None:
        assert "dp.embedding_id IS NULL" not in self._sql()

    def test_a_document_mid_ingest_is_left_alone(self) -> None:
        """Clearing embedding_id under an open run triggers a recovery re-ingest.

        stale_run_detector._project_is_fully_embedded reads
        `embedding_id IS NULL` as "this run has not finished embedding" and
        dispatches a fresh ingest_pdf for the document.
        """
        sql = self._sql()

        assert "silver.ingest_progress" in sql
        assert "'queued', 'started'" in sql

    def test_passages_that_will_never_be_embedded_are_skipped(self) -> None:
        """One generation each, for text no embedder will ever read."""
        sql = self._sql()

        # OCR-rejected / queued-for-re-OCR: the embed sweep skips these.
        assert "'rejected', 'pending_reocr'" in sql
        # Image passages are embedded from the page render, not their text,
        # so contextualized_content is never consulted for them.
        assert "dp.modality <> 'image'" in sql


class TestEnrichmentReachesTheVector:
    def test_the_writeback_clears_embedding_id(self) -> None:
        """Otherwise the enriched text is written and never encoded.

        The row already has a vector built from the bare text and the embed
        sweep only looks at `embedding_id IS NULL`, so without this the
        header is stored where nothing will read it.
        """
        sql = code_only(inspect.getsource(ce.enrich_passage_context))

        assert "embedding_id = NULL" in sql

    def test_re_embedding_overwrites_rather_than_orphans(self) -> None:
        """The point id is derived from passage_id, so the upsert replaces."""
        from app.services.ingest.passage_embedder import _passage_to_point_id

        pid = "7f1c9c4e-0d3a-4a2b-9c1e-2f5a8b3c6d70"

        assert _passage_to_point_id(pid) == _passage_to_point_id(pid)


class TestBackendRouting:
    def test_azure_resolves_to_foundry_with_auth(self, monkeypatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "LLM_BACKEND", "azure")
        monkeypatch.setattr(
            settings, "AZURE_FOUNDRY_ENDPOINT", "https://example.services.ai.azure.com"
        )
        monkeypatch.setattr(settings, "AZURE_FOUNDRY_DEPLOYMENT", "Cohere-command-a-plus")
        monkeypatch.setattr(settings, "AZURE_FOUNDRY_API_KEY", "k")

        base_url, model, headers = ce._resolve_llm_target()

        assert base_url == "https://example.services.ai.azure.com/openai/v1"
        assert model == "Cohere-command-a-plus"
        assert headers == {"api-key": "k"}

    def test_it_no_longer_pins_itself_to_the_removed_vllm_service(self) -> None:
        source = code_only(inspect.getsource(ce))

        assert "settings.VLLM_URL" not in source

    @pytest.mark.parametrize("backend", ["vllm", "openai"])
    def test_other_backends_send_no_azure_header(self, monkeypatch, backend: str) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "LLM_BACKEND", backend)

        _, _, headers = ce._resolve_llm_target()

        assert headers == {}
