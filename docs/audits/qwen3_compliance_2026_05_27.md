# Qwen3 citation compliance benchmark — 2026-05-27

**Plan reference:** §0c (decision gate for §4b citation guards)
**Model:** `Qwen/Qwen3-14B-AWQ` (live production, via vLLM at `http://vllm:8000`)
**Total runtime:** 11 min 19 s for 100 trials
**Run trigger:** Job 2 wiring complete → §0c gate decision needed before §4b

## Decision gate verdict

Plan §0c, verbatim: *"If citation compliance rate < 85% in Test 1, the system prompt must be redesigned before any citation guard implementation proceeds."*

**Test 1 PASSED at ≥85% compliance. Plan §4b citation guards are UNBLOCKED.**

## Results

| # | Test | Trials | Outcome | Compliance |
|---|---|---:|---|---:|
| 1 | Basic citation production | 20 | **PASSED** | ≥85% |
| 2 | Numeric citation grounding | 20 | **PASSED** | ≥85% |
| 3 | No hallucinated doc indices | 20 | **PASSED** | ≥85% |
| 4 | Multi-document citation (5 docs, expect ≥3 distinct cites) | 10 | **FAILED** | 0% |
| 5 | Long-context drift (~5.5 k tok context, target in first 1 k) | 10 | **FAILED** | 0% |
| 6 | Citation placement under structured-answer format | 20 | **PASSED** | ≥85% |

4 of 6 tests pass. The two failures (4 + 5) deserve investigation but do NOT gate plan §4b — only Test 1 does.

## On the two failures

### Test 4 — multi-document (0/10 compliance)

Setup: 5 documents provided, query asks for a 3-part summary (resource + intercept + property location), assertion requires ≥3 distinct documents cited with `[doc:N]` markers.

Observed: zero `[doc:N]` markers across all 10 trials. The model produced prose talking ABOUT "Document 3" and "Document 4" but did NOT emit the bracketed citation format.

Likely root cause: the 400-token response cap was set in the test scaffold. Qwen3-14B's "thinking mode" generates verbose `<think>...</think>` reasoning blocks that consume the response budget before the actual answer with citations gets emitted. The truncated raw_response fragments captured in the log all end mid-`<think>` block — the actual answer wasn't even generated within the cap.

Fix: bump `max_tokens` to 800–1000 for tests 4 + 6 (which expect richer answers), OR disable thinking mode via `chat_template_kwargs={"enable_thinking": false}` in the request. The orchestrator's production path already handles this (see `test_qwen3_payload_shape.py` and `project_qwen3_payload_shape.py`).

Pre-fix verdict: artifact of the test scaffold, NOT a model deficiency.

### Test 5 — long-context drift (0/10 compliance)

Setup: target document with the answer in position 1, followed by 8 padding documents (~5.5 k total tokens). Query asks for the Au grade from the target doc.

Observed: 0/10 trials produced both the correct citation AND the value `2.31`. Same pattern as Test 4 — `<think>` blocks consuming the response budget.

Pre-fix verdict: same scaffold artifact as Test 4.

## On the 6 teardown errors

All 6 ERRORs are at teardown of the `vllm_client` fixture:

```python
@pytest.fixture
def vllm_client():
    ...
    yield client
    import asyncio
    asyncio.run(client.aclose())   # ← bug: loop already closed by pytest_asyncio
```

`pytest_asyncio` closes its event loop before sync-fixture teardown runs, so `asyncio.run()` fails with `RuntimeError: Event loop is closed`. The fixture should be `@pytest_asyncio.fixture` and use `await client.aclose()`.

Bug originated in the overnight scaffold (commit `4aba659`). Doesn't affect test outcomes — happens after the test body's assert evaluates.

Fix: convert to async fixture (1-line change).

## What this means for plan §4b

Plan §4b (citation repair loop with structured error codes — 16-code `GuardErrorCode` enum + repair strategy per code) is unblocked. Citation compliance is solid for the cases that matter most:

- ✅ **Single-document factual lookup** — Test 1
- ✅ **Numeric value + unit + citation** — Test 2 (the hallucination-prevention bedrock)
- ✅ **Refusal-on-unsupported** — Test 3 (won't invent doc indices)
- ✅ **Structured-format placement** — Test 6 (citations land inside the Evidence section)

The multi-doc and long-context regimes need the scaffold fix + re-run before we have full picture, but those failures are diagnostically NOT model-side.

## Follow-up tasks (NOT blocking)

1. Fix the `vllm_client` fixture (async). 5-line change in `src/fastapi/tests/test_qwen3_citation_compliance.py`.
2. Bump `max_tokens` in Tests 4 + 5 from 400 → 1000. OR add `enable_thinking=false` to the payload.
3. Re-run JUST Tests 4 + 5 after the fix (~3 min). If they pass → full bench is green.
4. Wire the audit into a quarterly Hatchet cron so this baseline can be re-measured as prompts evolve.

## Citation format observed

Sanity-call sample response (from the pre-flight, 3-doc setup, "Where was hole ECK-22-001 assayed?"):

```
<think>
Okay, let's see. The user is asking where hole ECK-22-001 was assayed and what method was used.
...
</think>

Hole ECK-22-001 was assayed at Activation Laboratories using a fire assay with AA finish
(code Au-AA23) [doc:2 p:42].
```

Citation format `[doc:N p:P]` matches the prompt instruction verbatim. The model honors:
- Document number (`2`)
- Page reference (`42`)
- Inline placement after the factual claim

This is the citation format the §4b guards will validate against.

## Source data

- Raw log: `/c/Users/GeoRAG/AppData/Local/Temp/qwen3_compliance.log` (transient — not committed)
- Test fixtures: `src/fastapi/tests/test_qwen3_citation_compliance.py` (commit `4aba659`, model name fixed in `ee828e1`)
- Runner: `src/fastapi/scripts/run_qwen3_citation_compliance.py`

---

_Generated 2026-05-27 from `pytest tests/test_qwen3_citation_compliance.py` inside the fastapi container with `QWEN3_COMPLIANCE_MANUAL=1` + `VLLM_BASE_URL=http://vllm:8000` + `VLLM_MODEL=Qwen/Qwen3-14B-AWQ`._

---

## Update — 2026-05-27, scaffold fixes applied + re-runs complete

After the initial bench (above), the three follow-up tasks were executed:

### 1. Scaffold fix — async fixture + max_tokens bump (commit `e3923e4`)

- `vllm_client` fixture converted to `@pytest_asyncio.fixture` with `await client.aclose()` in `try/finally`. Eliminates the 6 teardown ERRORs.
- `_one_shot`'s `max_tokens` bumped 400 → 1200 (Tests 1–5).
- Test 6's inline payload bumped `max_tokens` 600 → 1500 (longer structured-format answers).

### 2. Test 4 re-run with scaffold fix — PASSED

Tests 4 + 5 re-run (3 min 02 s). Test 4 **passed** at ≥85% compliance, confirming the original hypothesis: max_tokens=400 was too tight for Qwen3-14B's `<think>...</think>` reasoning + multi-doc answer. With 1200, the model produces both the reasoning AND the cited answer with `[doc:N]` markers across 3+ documents.

### 3. Test 5 fixture bug discovered + fixed (commit `a25cfa2`)

Test 5 STILL failed at 0% after the scaffold fix — but for a DIFFERENT reason. Root cause:

The shared fixture `_DOC_B` has the in-document header **`"[Document 2 — Sampling & Analysis,"`** because it occupies position 2 in the 3-doc setups of Tests 1–4. In Test 5's long-context setup it's at LIST position 1, but the in-document label still says "Document 2". Qwen3 correctly reads the label and emits `[doc:2]` citations. The test asserted `[doc:1]`. The model was right; the test was wrong.

Fix: per-test fixture transform that rewrites `[Document 2 —` → `[Document 1 —` for Test 5 only. Other tests untouched.

### 4. Test 5 re-run with fixture fix — PASSED

Test 5 alone re-run (65 s) — **passed** at ≥85% compliance.

### Final verdict — FULL BENCH GREEN

| # | Test | Trials | Final outcome |
|---|---|---:|---|
| 1 | Basic citation production | 20 | **PASSED** |
| 2 | Numeric citation grounding | 20 | **PASSED** |
| 3 | No hallucinated doc indices | 20 | **PASSED** |
| 4 | Multi-document citation (5 docs) | 10 | **PASSED** (after `e3923e4`) |
| 5 | Long-context drift | 10 | **PASSED** (after `a25cfa2`) |
| 6 | Structured-format placement | 20 | **PASSED** |
| | **Total** | **100** | **6/6 PASS** |

Qwen3-14B-AWQ is fit for the plan §4b citation-guard arm across all six tested regimes. The compliance audit is now usable as a quarterly regression baseline.

### Source data (updated)

- Run 1 (full bench): 11:19, 4 PASS / 2 FAIL / 6 teardown ERRORs — `qwen3_compliance.log`
- Run 2 (tests 4 + 5 only, after scaffold fix): 3:02, 1 PASS / 1 FAIL — `qwen3_compliance_t4t5.log`
- Run 3 (test 5 only, after fixture fix): 1:05, 1 PASS — `qwen3_compliance_t5.log`
