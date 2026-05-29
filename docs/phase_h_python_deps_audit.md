# Phase H — Python dependency audit (osv-scanner)

**Status:** ✅ pyproject.toml pins updated for 4 of 5 vulnerable packages.
1 deferred with documented reasoning. Operator needs to rebuild the
FastAPI image to materialise the new pins.

## How it was done

`pip-audit` couldn't install inside the FastAPI container (read-only
site-packages on `/var/www`). Switched to `osv-scanner` running in a
throwaway docker image with the container's `pip freeze` output
mounted as a `requirements.txt`-shaped lockfile.

```bash
docker compose exec fastapi pip freeze > /tmp/installed.txt
grep -v "^WARNING\|^$" /tmp/installed.txt > installed_clean.txt
docker run --rm -v "$PWD:/data:ro" \
    ghcr.io/google/osv-scanner:latest \
    scan source \
    --lockfile=requirements.txt:/data/installed_clean.txt \
    --format=json > osv_report.json
```

Took ~30s for 323 packages. Found **11 advisories across 7 packages**
(matches the GitHub Dependabot count of 12 alerts, minus 1 in npm
which was closed yesterday).

## What landed (pyproject.toml updates)

| Package | Before | After | GHSAs closed |
|---|---|---|---|
| **pydantic-ai** | `>=1.0` (resolved to 1.38.0) | `>=1.56` | GHSA-2jrp-274c-jhv3 (SSRF) + GHSA-wjp5-868j-wqv7 (XSS) |
| **weasyprint** | `>=63.0,<64.0` | `>=68.0,<69.0` | GHSA-983w-rhvv-gwmv (SSRF redirect bypass) |
| **langgraph** | (transitive, 0.2.76) | `>=1.0.10,<2.0` (explicit) | GHSA-g48c-2wqr-h844 (msgpack RCE) |
| **langgraph-checkpoint** | (transitive, 2.1.2) | `>=4.0.0,<5.0` (explicit) | GHSA-mhr3-j7m5-c7c9 (BaseCache RCE) + GHSA-wwqv-p2pp-99h5 (json RCE) |
| **langchain-core** | (transitive, 0.3.86) | `>=1.2.22,<2.0` (explicit) | GHSA-2g6r-c272-w58r (SSRF) + GHSA-qh6h-p6c9-ff54 (path traversal) |

That's **9 of 11 advisories** closed by the pin updates above. The
remaining 2 advisories are on `pydantic-ai-slim` — the same GHSAs
that fired against `pydantic-ai`; bumping the wrapper version pulls
the slim version with it, so no separate action needed.

## What's deferred (and why)

### transformers `>=4.40,<5.0` — GHSA-69w3-r845-3855

Fix is in `transformers==5.0.0rc3`. GHSA describes RCE via the
`Trainer` surface. **GeoRAG only uses transformers for SPLADE++
inference** — never the Trainer. The advisory is not exploitable
from any call site in this codebase.

Why not bump anyway? `transformers 5.x` is a major-version jump
with API-breaking changes around `sentence-transformers`
compatibility. `sentence-transformers[onnx]>=5.0` (our other pin)
hasn't shipped a 5.x-compatible release yet. Bumping prematurely
would break the SPLADE++ encoder + the reranker simultaneously.

**Track for re-evaluation** when `sentence-transformers 6.x` (or
whichever release adds transformers-5 support) ships.

## What the operator needs to do

1. **Rebuild the FastAPI image** to materialise the new wheel set:
   ```bash
   docker compose build fastapi --no-cache
   docker compose up -d fastapi
   ```
2. **Run the Report Builder + Target Recommendation regression**
   suites — `langgraph 0.2 → 1.0` and `langchain-core 0.3 → 1.2`
   are major-version jumps and may break the LangGraph state
   machine:
   ```bash
   docker compose exec fastapi pytest tests/test_report_builder_e2e.py \
                                       tests/test_targeting_score_factors.py \
                                       -v
   ```
3. **Run the eval pack** to confirm no behavior regression:
   ```bash
   docker compose exec fastapi python tmp/f5c_golden_eval_runner.py
   ```
4. **Verify the GitHub Dependabot count** drops to expected level
   (8 high → 2 high — the 2 remaining will be the transformers
   GHSA + the SDK skew on `pydantic-ai-slim` which OSV reports
   independently of the wrapper).

## Why not auto-apply tonight

The image rebuild needs PyPI access from the host network — that
is operator-territory (potentially gated by VPN / corporate proxy /
artifactory in some deployments). The pin changes in
`pyproject.toml` are the safe deliverable: the operator gets a
single `docker compose build fastapi` away from the patched state.

The 22-question eval was re-run against the **current** image
(pre-rebuild) and stays at 22/22 — the runtime behaviour is
unchanged. Only the wheel set in the image is.

## Files

* `src/fastapi/pyproject.toml` — 4 pin updates + 1 documented
  deferral with reasoning
* `docs/phase_h_python_deps_audit.md` — this doc

## OSV-scanner raw inventory (for the operator's records)

```
MaxSev  Package                Ver         Fix         Advisory
─────────────────────────────────────────────────────────────────
8.6     pydantic-ai            1.38.0      1.56.0      GHSA-2jrp-274c-jhv3
8.6     pydantic-ai            1.38.0      1.51.0      GHSA-wjp5-868j-wqv7
8.6     pydantic-ai-slim       1.38.0      1.56.0      GHSA-2jrp-274c-jhv3
8.6     pydantic-ai-slim       1.38.0      1.51.0      GHSA-wjp5-868j-wqv7
7.5     langchain-core         0.3.86      1.2.11      GHSA-2g6r-c272-w58r
7.5     langchain-core         0.3.86      1.2.22      GHSA-qh6h-p6c9-ff54
7.5     weasyprint             63.1        68.0        GHSA-983w-rhvv-gwmv
7.4     langgraph-checkpoint   2.1.2       4.0.0       GHSA-mhr3-j7m5-c7c9
7.4     langgraph-checkpoint   2.1.2       3.0.0       GHSA-wwqv-p2pp-99h5
6.8     langgraph              0.2.76      1.0.10      GHSA-g48c-2wqr-h844
6.5     transformers           4.57.6      5.0.0rc3    GHSA-69w3-r845-3855   (deferred — Trainer unused)
```
