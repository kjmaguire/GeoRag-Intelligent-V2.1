#!/usr/bin/env python3
"""Qwen model-config drift guard.

Cross-file values drifted apart between 2026-04-21 and 2026-04-27 because
they were maintained in several places (`.env.example`,
`docker-compose.yml`, FastAPI `config.py`). This script re-asserts the
single-source-of-truth contract so the next drift fails CI rather than
silent-shipping. (The Ollama half of this guard -- OLLAMA_NUM_CTX,
OLLAMA_KEEP_ALIVE -- was removed with the Ollama service on 2026-05-17;
those checks compared None to None and could never fail.)

What it checks (and why):

  1. `LLM_PRIMARY_MODEL` default in `docker-compose.yml` matches the
     value in `.env.example`.  Drift here means an empty `.env` boots a
     different model silently.

  2. `MAX_CONTEXT_TOKENS` in `.env.example` matches the default in
     `src/fastapi/app/config.py`.  Operators copying `.env.example` to
     `.env` would otherwise configure FastAPI differently from the code
     default.

Exits 0 on consistency, prints a per-mismatch report and exits 1
otherwise. Standalone -- only depends on stdlib + PyYAML.

Run locally:
    python scripts/check_qwen_config_drift.py

Wired into CI as a separate `qwen-config-drift` job in
`.github/workflows/ci.yml` so a failure surfaces clearly distinct from
unit-test failures.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

ENV_EXAMPLE = REPO_ROOT / ".env.example"
COMPOSE = REPO_ROOT / "docker-compose.yml"
CONFIG_PY = REPO_ROOT / "src" / "fastapi" / "app" / "config.py"


# `${VAR:-default}` → captures the default after the `:-`.
COMPOSE_DEFAULT = re.compile(r"\$\{[A-Z_0-9]+:-([^}]*)\}")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _env_value(env_text: str, key: str) -> str | None:
    """First non-comment `KEY=value` line — that's the active default."""
    for line in env_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(f"{key}="):
            return stripped.split("=", 1)[1].strip()
    return None


def _compose_env_default(compose_doc: dict, service: str, key: str) -> str | None:
    """Read `services.<service>.environment.<key>` and extract the
    `${KEY:-default}` default. Returns None if the key isn't present
    or doesn't have an inline default."""
    svc = compose_doc.get("services", {}).get(service, {})
    env = svc.get("environment", {}) or {}
    raw = env.get(key)
    if raw is None:
        return None
    raw_str = str(raw)
    m = COMPOSE_DEFAULT.search(raw_str)
    if m is None:
        # Plain literal value — also count this as the default.
        return raw_str.strip()
    return m.group(1).strip()


def _config_int(config_text: str, attr: str) -> int | None:
    """Find a `Settings` attribute declaration like `ATTR: int = 1234`
    and return the int. Tolerant to underscore digit grouping."""
    pat = re.compile(
        rf"^\s*{re.escape(attr)}\s*:\s*int\s*=\s*([\d_]+)",
        re.MULTILINE,
    )
    m = pat.search(config_text)
    if m is None:
        return None
    return int(m.group(1).replace("_", ""))


def main() -> int:
    env_text = _read_text(ENV_EXAMPLE)
    compose_doc = yaml.safe_load(_read_text(COMPOSE))
    config_text = _read_text(CONFIG_PY)

    failures: list[str] = []

    # ── Check 1: LLM_PRIMARY_MODEL ────────────────────────────────────────
    env_model = _env_value(env_text, "LLM_PRIMARY_MODEL")
    compose_model = _compose_env_default(compose_doc, "fastapi", "LLM_PRIMARY_MODEL")
    if env_model != compose_model:
        failures.append(
            f"LLM_PRIMARY_MODEL drift: .env.example={env_model!r} "
            f"vs docker-compose.yml fastapi.environment={compose_model!r}. "
            f"An empty .env boots the compose default — these MUST match."
        )

    # ── Check 2: MAX_CONTEXT_TOKENS (.env.example vs config.py) ───────────
    env_max_ctx_str = _env_value(env_text, "MAX_CONTEXT_TOKENS")
    config_max_ctx = _config_int(config_text, "MAX_CONTEXT_TOKENS")
    try:
        env_max_ctx = int(env_max_ctx_str) if env_max_ctx_str else None
    except ValueError:
        env_max_ctx = None

    if env_max_ctx is not None and config_max_ctx is not None and env_max_ctx != config_max_ctx:
        failures.append(
            f"MAX_CONTEXT_TOKENS drift: .env.example={env_max_ctx} "
            f"vs config.py={config_max_ctx}. Operators copying .env.example "
            "to .env will configure FastAPI differently than the code default."
        )

    # ── Report ────────────────────────────────────────────────────────────
    if failures:
        print("Qwen config drift guard: FAIL", file=sys.stderr)
        print("", file=sys.stderr)
        for f in failures:
            print(f"  ✗ {f}", file=sys.stderr)
            print("", file=sys.stderr)
        return 1

    print("Qwen config drift guard: OK")
    print(f"  LLM_PRIMARY_MODEL    = {env_model}")
    print(f"  MAX_CONTEXT_TOKENS   = {env_max_ctx} (env.example) / {config_max_ctx} (config.py)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
