# =============================================================================
# Langfuse MCP server — Docker image for the georag MCP profile
# =============================================================================
# Wraps avivsinai/langfuse-mcp (PyPI: langfuse-mcp) so it can be registered
# with Claude Code via stdio MCP. Speaks MCP over stdio (the gateway picks
# up stdio I/O).
#
# Why Langfuse vs LangSmith:
#   * Apache 2.0 — no SaaS lock-in, fully self-hostable
#   * Aligns with GeoRAG's air-gapped junior-mining on-prem target
#   * Free for the user (no SaaS subscription needed for dev)
#   * Self-host shape: docker/compose.langfuse.yml overlay
#
# Why a custom image (vs running pip install on the host):
#   * Isolated runtime — credentials only visible to the MCP container
#   * Reproducible across machines/operators
#   * Same pattern as our other MCP wrapper images
#
# Build:
#   docker build -f docker/langfuse-mcp.Dockerfile -t georag/langfuse-mcp:latest .
#
# Required env vars at runtime (set via Claude Code's claude_desktop_config.json
# or via Docker MCP secrets):
#   LANGFUSE_PUBLIC_KEY   — from Langfuse UI → Settings → API Keys
#   LANGFUSE_SECRET_KEY   — from Langfuse UI → Settings → API Keys
#   LANGFUSE_HOST         — http://host.docker.internal:3001  for local self-host
#                           https://cloud.langfuse.com         for SaaS free tier
#
# Pinned base — python:3.13-slim. Bump on security advisories or pyproject change.
# =============================================================================

FROM python:3.13-slim

LABEL org.opencontainers.image.title="GeoRAG Langfuse MCP"
LABEL org.opencontainers.image.description="Langfuse MCP server (avivsinai/langfuse-mcp) packaged for Docker MCP / Claude Code stdio."
LABEL org.opencontainers.image.source="https://github.com/avivsinai/langfuse-mcp"
LABEL org.opencontainers.image.vendor="GeoRAG (georag-internal)"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Install the Langfuse MCP server. Pin the version so a silent
# upstream change can't break the gateway integration mid-session.
# Bump deliberately when avivsinai ships a feature you want.
ARG LANGFUSE_MCP_VERSION=0.9.1
RUN pip install --no-cache-dir "langfuse-mcp==${LANGFUSE_MCP_VERSION}"

# Non-root for least-privilege. The MCP server reads from stdin/stdout
# and makes outbound HTTP(S) to the Langfuse API — no filesystem writes.
RUN useradd --system --no-create-home --uid 65532 mcp
USER mcp

# stdio entry point. The langfuse-mcp CLI provides this directly.
# Docker MCP Gateway / Claude Code stdio runs this with stdin attached
# and reads MCP frames from stdout.
ENTRYPOINT ["langfuse-mcp"]
