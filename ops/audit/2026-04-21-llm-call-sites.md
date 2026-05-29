# LLM Call-Site Audit — Module 5 Chunk 2
**Date:** 2026-04-21
**Engineer:** backend-fastapi agent
**Scope:** Every Ollama/OpenAI-compat or Anthropic LLM call in `src/fastapi/app/`
**Purpose:** Classify each call site as free-text, structured, or hybrid/unclear before
applying `enable_thinking=False` + `format="json"` overrides to structured paths.

---

## Method

Two grep passes on `src/fastapi/app/**/*.py` (excluding `__pycache__`):

```bash
grep -rn 'chat/completions\|_call_openai_compatible_llm\|_call_anthropic_llm\|_call_llm\|messages\.create' \
  src/fastapi/app --include='*.py' | grep -v __pycache__
```

```bash
grep -rn 'Agent\[|Agent(\|model=settings\.' src/fastapi/app --include='*.py' | grep -v __pycache__
```

---

## Call-Site Inventory

| # | File:line | Kind | Description | Current extra_body | Planned override |
|---|---|---|---|---|---|
| CS-01 | `agent/orchestrator.py:1145` `_call_openai_compatible_llm` (definition) | **free-text** | Main synthesis path for Ollama/vLLM. Returns natural-language answer. Receives `enable_thinking` param from env default. Caller = `_call_llm`, deepseek failover path. | None | Add `enable_thinking` param with env default; inject `chat_template_kwargs` into payload. No `format="json"` — this is a free-text path. |
| CS-02 | `agent/orchestrator.py:1364` `_call_anthropic_llm` (definition) | **free-text** | Native Anthropic SDK path. Returns natural-language text (plain messages.create). Not OpenAI-compat — no `extra_body` concept. Anthropic thinking mode is controlled by the `thinking` param in the SDK, not `chat_template_kwargs`. | N/A (Anthropic SDK) | No change needed for Qwen3 migration — this path is Anthropic-only and is not affected by GGUF thinking mode. |
| CS-03 | `agent/orchestrator.py:1624` `_call_llm(...)` (primary synthesis call in retry loop) | **free-text** | Dispatch shim. Routes to CS-01 or CS-02. Inherits the free-text nature of both targets. `audit_label="primary"`. | None (dispatch only) | No change — dispatcher only. |
| CS-04 | `agent/orchestrator.py:3705` `_call_llm(...)` (failover retry in synthesis loop) | **free-text** | Validation-fail retry. Passes `correction_hint` from the hallucination validator. Same free-text output expected. `audit_label="retry"`. | None | No change — dispatcher only. |
| CS-05 | `agent/orchestrator.py:3764` `_call_openai_compatible_llm(...)` (deepseek failover — direct call, bypasses _call_llm) | **free-text** | Anthropic → OpenAI-compat cross-backend failover. Returns natural-language text. Builds user_message from `_build_user_message` before calling. | None | Add `enable_thinking=False` via `chat_template_kwargs` — this call goes to vLLM/Ollama without thinking, consistent with the free-text default. Actually: this IS a free-text path but is called in a fallback context. Use the env-default via the `_call_openai_compatible_llm` signature parameter (CS-01 carries it). |
| CS-06 | `agent/orchestrator.py:3829` `_call_anthropic_llm(...)` (Ollama fallback → Anthropic) | **free-text** | Ollama failure fallback to Anthropic STANDARD tier. Returns natural-language text. Native Anthropic SDK. | N/A (Anthropic SDK) | No change — Anthropic SDK path, not GGUF. |
| CS-07 | `agent/llm_classifier.py:123` `anthropic_client.messages.create(...)` | **structured** | LLM-based query routing classifier. Outputs a JSON dict of bucket booleans. Parsed by `_parse_classifier_json`. Uses Anthropic SDK natively — not OpenAI-compat. `temperature=0.0`, `max_tokens=200`. | N/A (Anthropic SDK) | Anthropic SDK does not use `extra_body`/`chat_template_kwargs`. This path is Anthropic-only and JSON is enforced by the system prompt instruction ("Output only valid JSON, nothing else"). No Qwen3 concern. |
| CS-08 | `agent/escalation.py:123` `anthropic_client.messages.create(...)` | **structured** | Query rephrasing LLM call. Outputs `{"rephrasings": [...]}` JSON. Parsed by `_parse_rephrasings_json`. Anthropic SDK native. `temperature=0.4`, `max_tokens=400`. | N/A (Anthropic SDK) | Same as CS-07 — Anthropic-only, not GGUF. System prompt instructs JSON-only output. No Qwen3 concern. |
| CS-09 | `agent/agentic_escalation.py:112` `Agent(model=AnthropicModel(...))` | **structured** | Pydantic AI agent for third-tier escalation. Tool-calling agent. Uses `AnthropicModel` (Anthropic SDK). Tool return types are typed dataclasses. | N/A (Anthropic SDK) | Anthropic-only Pydantic AI agent. Not an OpenAI-compat path. No Qwen3 `enable_thinking` concern. |

---

## Summary

**Total call sites: 9**

| Kind | Count | Sites |
|---|---|---|
| free-text | 6 | CS-01, CS-02, CS-03, CS-04, CS-05, CS-06 |
| structured | 3 | CS-07, CS-08, CS-09 |
| hybrid/unclear | 0 | — |

### Key finding: Ollama path is entirely free-text

All three structured LLM paths (classifier, rephrasing, agentic escalation) use the
**Anthropic SDK natively** — not the OpenAI-compatible endpoint that reaches Ollama.
The `enable_thinking=False` + `format="json"` override pattern applies only to
OpenAI-compatible (Ollama/vLLM) call sites.

The **only OpenAI-compatible call site** is `_call_openai_compatible_llm` (CS-01),
which is a free-text synthesis path. No structured JSON is parsed from its output.

### Planned action

- **CS-01 (`_call_openai_compatible_llm`):** Add `enable_thinking: bool` parameter
  defaulting to `bool(os.getenv("ENABLE_THINKING", "true").lower() == "true")`.
  Inject into `request_payload["chat_template_kwargs"]`. This is the main free-text
  path — `enable_thinking=True` is the correct default. No `format="json"` override.

- **CS-07, CS-08, CS-09:** Anthropic SDK paths. `chat_template_kwargs` is not a
  concept on the Anthropic API. These paths are inherently structured via the system
  prompt instruction and SDK response shape. No change needed for Qwen3 migration.

- **CS-02, CS-06:** Anthropic native SDK free-text paths. No change.

- **CS-03, CS-04, CS-05:** Dispatch shims or free-text paths to CS-01. The
  `enable_thinking` flag propagates through CS-01's signature. No additional override.

### Conclusion: 1 call site patched (CS-01 — free-text with env-default thinking param)

Since no OpenAI-compat path produces structured JSON output, there are **zero**
structured Ollama/vLLM call sites requiring `enable_thinking=False` + `format="json"`.

The structured paths (classifier, rephrasing, agentic escalation) all go through the
Anthropic SDK, which does not have `chat_template_kwargs`. Those paths are insulated
from Qwen3 thinking-mode leakage by design.

This finding aligns with the Chunk 1 validator result: `supports_thinking=false` on
every model across all prompts at current Ollama 0.21.0. The `enable_thinking` hook on
CS-01 is forward-looking scaffolding for when/if a future Ollama build enables it.

---

*Audit complete. No structured Ollama paths found. 1 free-text path (CS-01) patched
with `enable_thinking` parameter. Anthropic SDK paths unaffected by Qwen3 migration.*
