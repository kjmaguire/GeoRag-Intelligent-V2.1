#!/usr/bin/env python3
"""Qwen / Ollama configuration drift guard.

Four cross-file values drifted apart between 2026-04-21 and 2026-04-27
because they were maintained in three places (`.env.example`,
`docker-compose.yml` ollama service, FastAPI `config.py`). This script
re-asserts the single-source-of-truth contract so the next drift fails
CI rather than silent-shipping a `MAX_CONTEXT_TOKENS` larger than
Ollama's actual `num_ctx`.

What it checks (and why):

  1. `LLM_PRIMARY_MODEL` default in `docker-compose.yml` matches the
     value in `.env.example`.  Drift here means an empty `.env` boots
     the rollback model silently.

  2. `OLLAMA_NUM_CTX` default in `docker-compose.yml` matches
     `OLLAMA_NUM_CTX` in `.env.example` AND the Pydantic default in
     `src/fastapi/app/config.py`.  Drift here means the FastAPI side
     budgets more prompt than Ollama can serve, and Ollama silently
     truncates BEFORE the model sees the prompt.

  3. `MAX_CONTEXT_TOKENS` in `.env.example` is ≤ `OLLAMA_NUM_CTX`
     across the same files, with at least 500 tokens reserved for
     system prompt + completion overhead.  This is the invariant
     `MAX_CONTEXT_TOKENS < OLLAMA_NUM_CTX` and it can never go wrong
     in the safe direction.

  4. `OLLAMA_KEEP_ALIVE` default in `docker-compose.yml` matches
     `.env.example`.  Drift here is cosmetic but confuses the next
     reader of the cold-start latency math.

Exits 0 on consistency, prints a per-mismatch report and exits 1
otherwise. Standalone — only depends on stdlib + PyYAML.

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

    # ── Check 2: OLLAMA_NUM_CTX ───────────────────────────────────────────
    env_num_ctx_str = _env_value(env_text, "OLLAMA_NUM_CTX")
    compose_num_ctx_str = _compose_env_default(compose_doc, "ollama", "OLLAMA_NUM_CTX")
    config_num_ctx = _config_int(config_text, "OLLAMA_NUM_CTX")

    try:
        env_num_ctx = int(env_num_ctx_str) if env_num_ctx_str else None
        compose_num_ctx = int(compose_num_ctx_str) if compose_num_ctx_str else None
    except ValueError:
        env_num_ctx = compose_num_ctx = None

    values = {".env.example": env_num_ctx, "docker-compose.yml": compose_num_ctx, "config.py": config_num_ctx}
    if len({v for v in values.values() if v is not None}) > 1:
        failures.append(
            "OLLAMA_NUM_CTX drift across the three sources of truth: "
            + ", ".join(f"{k}={v}" for k, v in values.items())
            + ". Ollama silently truncates at its num_ctx BEFORE the model "
            + "sees the prompt — divergence here causes silent data loss."
        )

    # ── Check 3: MAX_CONTEXT_TOKENS < OLLAMA_NUM_CTX (with margin) ───────
    env_max_ctx_str = _env_value(env_text, "MAX_CONTEXT_TOKENS")
    config_max_ctx = _config_int(config_text, "MAX_CONTEXT_TOKENS")
    try:
        env_max_ctx = int(env_max_ctx_str) if env_max_ctx_str else None
    except ValueError:
        env_max_ctx = None

    target_num_ctx = env_num_ctx or compose_num_ctx or config_num_ctx
    SAFETY_MARGIN = 500  # system prompt + completion overhead
    for label, value in (("env.example", env_max_ctx), ("config.py", config_max_ctx)):
        if value is None or target_num_ctx is None:
            continue
        if value > target_num_ctx - SAFETY_MARGIN:
            failures.append(
                f"MAX_CONTEXT_TOKENS in {label}={value} exceeds "
                f"OLLAMA_NUM_CTX={target_num_ctx} − safety margin "
                f"{SAFETY_MARGIN} = {target_num_ctx - SAFETY_MARGIN}. "
                "Prompts will be silently truncated by Ollama."
            )

    # Also assert env.example and config.py agree with each other on
    # MAX_CONTEXT_TOKENS — they're the two places where developers tune it.
    if env_max_ctx is not None and config_max_ctx is not None and env_max_ctx != config_max_ctx:
        failures.append(
            f"MAX_CONTEXT_TOKENS drift: .env.example={env_max_ctx} "
            f"vs config.py={config_max_ctx}. Operators copying .env.example "
            "to .env will configure FastAPI differently than the code default."
        )

    # ── Check 4: OLLAMA_KEEP_ALIVE ────────────────────────────────────────
    env_keep = _env_value(env_text, "OLLAMA_KEEP_ALIVE")
    compose_keep = _compose_env_default(compose_doc, "ollama", "OLLAMA_KEEP_ALIVE")
    if env_keep is not None and compose_keep is not None and env_keep != compose_keep:
        failures.append(
            f"OLLAMA_KEEP_ALIVE drift: .env.example={env_keep!r} "
            f"vs docker-compose.yml={compose_keep!r}. Cold-start latency "
            "math in capacity-planning.md assumes one consistent value."
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
    print(f"  OLLAMA_NUM_CTX       = {target_num_ctx}")
    print(f"  MAX_CONTEXT_TOKENS   = {env_max_ctx} (env.example) / {config_max_ctx} (config.py)")
    print(f"  OLLAMA_KEEP_ALIVE    = {env_keep}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
