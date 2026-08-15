"""CI-only money-path smoke test support package.

Not part of the pytest suite (no test_*.py files here — the smoke test is
driven by scripts/ci/e2e_smoke_ingest.py + scripts/ci/e2e_smoke_query.py,
invoked directly as CI workflow steps, not via `pytest`). This package just
holds the stub backend server the smoke job boots to stand in for Azure AI
Foundry (embeddings + LLM chat-completions) without real credentials.

See the "E2E money-path smoke (ADVISORY)" job in .github/workflows/ci.yml
for the full pipeline this supports.
"""
