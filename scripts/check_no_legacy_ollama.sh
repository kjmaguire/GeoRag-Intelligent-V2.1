#!/usr/bin/env bash
# scripts/check_no_legacy_ollama.sh — Ollama-cutover guard (2026-05-18)
#
# Fails the commit if Ollama is reintroduced as a live LLM provider. The
# Ollama→vLLM migration completed 2026-05-10 (docs/model_migration.md) and
# the dead provider entry was removed from config/ai.php on 2026-05-18.
#
# This guard targets ONLY active-code reintroduction:
#   1. config/ai.php: a new 'ollama' => [ provider block
#   2. Live reads of OLLAMA_URL or OLLAMA_API_KEY env vars (PHP env() and
#      Python os.environ/settings) — those env vars no longer exist in .env
#   3. New `case 'ollama':` or `match 'ollama'` branches in PHP/Python
#      dispatch logic
#
# DELIBERATELY ALLOWED (so this guard doesn't false-positive on historical
# context):
#   * Literal["ollama", ...] type annotations in Python (DB read-compat for
#     historical answer_runs.backend rows)
#   * Comments and docstrings mentioning ollama
#   * docs/ markdown, ops/audit/ logs, CLAUDE.md, .claude/agents/*.md,
#     .ai/skills/**/*.md — all documentation
#   * The string "ollama" appearing in test fixtures, migration files,
#     and audit-log entries
#
# Run manually:
#   bash scripts/check_no_legacy_ollama.sh
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Forbidden patterns. Each is tight enough that adding it requires
# reintroducing Ollama as a live provider — not just mentioning it.
#
# PHP provider re-add:      'ollama' =>
# PHP env() reads:          env('OLLAMA_URL'|'OLLAMA_API_KEY'
# Python env reads:         os.environ[OLLAMA_ or settings.OLLAMA_URL etc.
# Live dispatch branches:   case 'ollama' (PHP/Python switch/match)
PATTERN_PHP="'ollama'[[:space:]]*=>|env\\(['\"]OLLAMA_(URL|API_KEY)|case[[:space:]]+['\"]ollama['\"]"
PATTERN_PY="os\\.environ\\[['\"]OLLAMA_|settings\\.OLLAMA_(URL|API_KEY)|match[[:space:]]+['\"]ollama['\"]"

# Restrict to active-code directories. Skip docs, audits, tests, archives,
# .claude/.ai meta dirs, migration logs, build outputs.
SEARCH_PATHS=(
  config
  app
  routes
  bootstrap
  src/fastapi/app
)

HITS=$(grep -rEHn "$PATTERN_PHP" "${SEARCH_PATHS[@]}" \
        --include='*.php' \
        --exclude-dir=node_modules --exclude-dir=vendor --exclude-dir=.git \
        2>/dev/null || true)
HITS_PY=$(grep -rEHn "$PATTERN_PY" "${SEARCH_PATHS[@]}" \
        --include='*.py' \
        --exclude-dir=__pycache__ --exclude-dir=.pytest_cache \
        2>/dev/null || true)

if [ -n "$HITS" ] || [ -n "$HITS_PY" ]; then
  echo "ERROR: Ollama is being reintroduced as a live LLM provider." >&2
  echo "The Ollama→vLLM cutover was completed 2026-05-10. See" >&2
  echo "docs/model_migration.md for context." >&2
  echo "" >&2
  [ -n "$HITS" ] && echo "$HITS" >&2
  [ -n "$HITS_PY" ] && echo "$HITS_PY" >&2
  echo "" >&2
  echo "If you genuinely need to revive Ollama support, this guard exists" >&2
  echo "to make that decision deliberate — discuss with Kyle first and" >&2
  echo "update docs/model_migration.md alongside." >&2
  exit 1
fi

exit 0
