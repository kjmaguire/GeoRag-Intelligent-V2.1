# pr/14 Follow-up Roadmap — issue templates + operator checklist

> Generated 2026-06-23 at the close of the version-audit sweep.
> Five follow-up workstreams; each is self-contained, has a clear
> definition of done, and points back to the ADR or runbook that
> covers the planning detail.
>
> Paste each section's ✂ block into a new GitHub Issue (or
> equivalent tracker) when you're ready to schedule the work.

---

## Operator deploy checklist (PR-14 → production)

When this PR merges and you're ready to deploy to staging/production,
walk this checklist. It captures the items that were scaffolded for
production safety but require operator action at deploy time.

- [ ] **Pull updated images.** `docker compose --env-file .env.production --profile dev-full pull` so the new SHAs (Caddy 2.11.3, Redis 8.6.4, SeaweedFS 4.35, Qdrant v1.17.1, Martin 1.11.0, Grafana 12.4.5, Loki 3.7.2, Tempo 2.10.7, ClickHouse 26.3-alpine, OTel 0.154.0, Langfuse 3 SHA-pinned, Hatchet-lite v0.86.12) land on the host.
- [ ] **Confirm `HATCHET_AUTH_COOKIE_INSECURE=f`**, `HATCHET_GRPC_INSECURE=f`, `HATCHET_AUTH_SET_EMAIL_VERIFIED=f` are present in `.env.production` (auto-applied via `.env.production.example` if you regenerated from the template). If `.env.production` predates this PR, copy the new block from `.env.production.example` lines 477-499.
- [ ] **Validate TLS on Hatchet gRPC.** With the INSECURE flags off, workers need `HATCHET_CLIENT_TLS_STRATEGY=tls` (or `mtls`) + a cert chain mounted. See the hatchet-lite block in compose for the env names.
- [ ] **Pre-stage Tesseract `eng.traineddata`** is NOT needed — it's now baked into the fastapi image. (Was previously stage-1 docling artifact.) However, **pre-stage PaddleOCR-VL weights** *only if* you plan to flip `PDF_DOCPARSER_BACKEND=paddleocr-vl` post-deploy: `huggingface-cli download PaddlePaddle/PaddleOCR-VL-1.6` to your air-gapped registry.
- [ ] **Rebuild postgres image once** to pick up the 6 pinned PG extensions (h3-pg v4.5.0, hypopg 1.4.3, pg_stat_kcache REL2_3_2, pg_partman v5.4.3, pg_repack ver_1.5.3, pg_ivm v1.14). Existing data volume is preserved — extensions are pure binary additions.
- [ ] **Verify Grafana 12.x dashboards render correctly.** The Scenes engine in 12.x occasionally re-renders panels slightly differently than 11.x. Spot-check the 14 system + 3 product dashboards; file follow-ups for any that need re-layout.
- [ ] **Confirm Cosign attestation lands on the next CI push.** `gh attestation list ghcr.io/<owner>/georag-fastapi:<sha>` after the first main-branch push post-merge. If missing, check the workflow's `id-token: write` permission scope.
- [ ] **Watch for Dependabot rescan.** Within 24 hours of merge, the 90-warning count on `main` should drop materially (~12 CVEs that were already addressed in pyproject but masked by the langgraph downgrade bug should auto-close).

---

## Follow-up #1 — Qwen3-VL-8B shadow-eval cutover

✂ ─────────────────────────────────────────────────────────────────────

### Title

Qwen3-VL-8B production cutover (post-pr/14 ADR-0015 Phase 2)

### Context

pr/14 landed scaffolding for the Qwen2.5-VL-7B → Qwen3-VL-8B migration: the `PDF_VL_MODEL_VERSION` env flag, the `_resolve_model_id()` helper in `src/fastapi/app/services/pdf_vl.py`, and the documented rollout plan in [`docs/adr/0015-qwen3-vl-8b-migration.md`](../adr/0015-qwen3-vl-8b-migration.md). Default is still `v2` (Qwen2.5-VL-7B). This issue covers the actual cutover.

### Acceptance criteria

- [ ] vLLM serving stand-up for Qwen3-VL-8B-Instruct-AWQ — either a second vLLM instance OR `PDF_VL_BACKEND_URL` override pointing at a dedicated VL serving box (decide based on A4500 VRAM budget per ADR-0015's "Phase 2 Risks" table).
- [ ] Pre-pull model weights into `vllm_hf_cache` (or equivalent for the dedicated instance) — ~10 GB AWQ-quantized.
- [ ] Shadow run for at least 7 days: dual-write 2.5-VL + 3-VL outputs on the same input pages, surfaced in `Admin/ShadowRuns`. Track schema-valid output rate (≥ 95%), figure→caption link rate vs baseline, per-page latency p95.
- [ ] Eval gate pass: `services/eval/promotion_gate.py` returns non-regression on the figure-grounded subset of `eval.golden_questions`.
- [ ] Flip `PDF_VL_MODEL_VERSION=3` in `.env.production` and update `.env.example` default.
- [ ] Append a `project_qwen3_vl_cutover_<date>.md` memory documenting the cutover date + observed metric deltas.

### Effort

~1-2 sessions. Most time is operational (model pull, shadow window monitoring) rather than coding.

### References

- [ADR-0015](../adr/0015-qwen3-vl-8b-migration.md)
- `src/fastapi/app/services/pdf_vl.py::_resolve_model_id`
- `Admin/ShadowRuns` Inertia page

✂ ─────────────────────────────────────────────────────────────────────

---

## Follow-up #2 — PaddleOCR-VL Phase 2 parser class

✂ ─────────────────────────────────────────────────────────────────────

### Title

PaddleOCR-VL-1.6 parser implementation + cutover gating (ADR-0016 Phase 2)

### Context

pr/14 landed Phase 1 of [ADR-0016](../adr/0016-paddleocr-3x-migration.md): paddleocr 2.10 → 3.7 with PP-OCRv5 as the new default for the regional-crop worker. The `paddleocr[doc-parser]` extra was added to pyproject and the `PDF_DOCPARSER_BACKEND` env flag is wired (default `docling`). Phase 2 is wiring the actual `PaddleOCRVLParser` class behind the flag.

### Acceptance criteria

- [ ] Write `src/fastapi/app/ocr/paddleocr_vl_parser.py` exposing a class with the same interface as the docling parser (input: PDF path or bytes; output: Markdown + figure regions + table regions matching the silver schema).
- [ ] Route the parser selection via `PDF_DOCPARSER_BACKEND` in whichever caller selects the document parser today (look in `app/services/ingest/` and `app/hatchet_workflows/ingest_pdf.py`).
- [ ] Pre-stage PaddleOCR-VL-1.6 weights: `huggingface-cli download PaddlePaddle/PaddleOCR-VL-1.6` — first-run download is ~3-4 GB and shouldn't happen during a hot ingest.
- [ ] Shadow run on a golden 20-PDF corpus (mix of scanned NI 43-101s, digital reports, table-heavy + chart-heavy). Compare Markdown structural fidelity, figure detection recall, per-page latency, VRAM peak.
- [ ] Decide on cutover heuristic: full swap, or per-document-class routing (`paddleocr-vl` for `scanned=true ∨ tables_detected>N`, `docling` otherwise).
- [ ] If routing-based: extend the parser dispatcher to inspect pre-parse signals (PDF metadata, sample-page heuristics) before picking a backend.

### Effort

~2-3 sessions. The parser class is meaty (interface mirroring + output-schema mapping), shadow run is operational, decision logic is small.

### References

- [ADR-0016 Phase 2 section](../adr/0016-paddleocr-3x-migration.md#what-changes-phase-2--proposed)
- [PaddleOCR-VL Usage Tutorial](https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PaddleOCR-VL.html)
- HF: [PaddlePaddle/PaddleOCR-VL-1.6](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6)

✂ ─────────────────────────────────────────────────────────────────────

---

## Follow-up #3 — Promtail → Alloy cutover

✂ ─────────────────────────────────────────────────────────────────────

### Title

Cut over from Promtail to Grafana Alloy + (optional) OTel-collector consolidation

### Context

pr/14 scaffolded the Alloy migration: the `alloy` service is in `docker-compose.yml` running v1.10.0 SHA-pinned alongside Promtail, with a translated config at `docker/alloy/config.alloy`. The plan is at [`ops/runbooks/promtail-to-alloy-migration.md`](../runbooks/promtail-to-alloy-migration.md). This issue is the actual cutover.

### Acceptance criteria

- [ ] Both Alloy + Promtail running in dev-monitor profile for ≥ 7 days; Loki receiving from both (Alloy writes to a separate tenant via the `tenant_id` line in `config.alloy`).
- [ ] Diff per-stream line counts + label cardinality between Alloy and Promtail. Match within 0.1%.
- [ ] `authz_audit` channel routing intact under Alloy (the per-event labels: `level`, `event`, `reason`, `target_workspace_id`).
- [ ] W3C `traceparent` stamping on Docker container logs intact.
- [ ] Decide minimal-vs-full-consolidation per the runbook's §"Decision points." If full: also translate `docker/otel-collector/config.yaml` into Alloy `otelcol.*` components and remove the OTel collector container.
- [ ] Promote Alloy to canonical tenant (delete the `tenant_id` line in `config.alloy`); stop the Promtail container; comment-out the `promtail:` service block in compose with a 1-week rollback window.
- [ ] Remove Promtail + `docker/promtail/` after the rollback window.

### Effort

~1 session for shadow setup + initial diff, ~1 session for cutover + (if full consolidation) OTel translation.

### References

- [Migration plan](../runbooks/promtail-to-alloy-migration.md)
- `docker/alloy/config.alloy` (current translated config)

✂ ─────────────────────────────────────────────────────────────────────

---

## Follow-up #4 — Dependabot triage on `main`

✂ ─────────────────────────────────────────────────────────────────────

### Title

Triage + close the 90 Dependabot vulnerabilities on `main`

### Context

GitHub reports 90 open Dependabot alerts on v21's `main` branch (1 critical, 23 high, 48 moderate, 18 low). pr/14 already addressed ~12 of these by extension (the CVEs that were ALREADY fixed in pyproject but masked by the Dockerfile langgraph-downgrade bug — see the bottom of pr/14's PR description). The rest need explicit triage.

### Acceptance criteria

- [ ] Pull the full alert list: `gh api -X GET "repos/kjmaguire/GeoRag-Intelligent-V2.1/dependabot/alerts?state=open&per_page=100" --jq '.[] | {pkg: .dependency.package.name, eco: .dependency.package.ecosystem, sev: .security_advisory.severity, fix: .security_vulnerability.first_patched_version.identifier}' | sort -u > /tmp/dependabot.json`
- [ ] Bucket by ecosystem: npm vs pip vs composer vs github-actions vs docker. Each bucket likely needs its own focused PR.
- [ ] For each bucket: identify which alerts are addressable by a one-line version bump in the relevant manifest, which need code changes, which are false positives (e.g. dev-only deps not shipped in production images).
- [ ] Mark "Won't fix" alerts (e.g. transitive dev-only deps the production image strips) with a justification per `gh api -X PATCH .../alerts/<n>` — keeps the Dependabot count honest.
- [ ] Address the critical + high tier first (1 + 23 = 24 alerts). Moderate + low can wait.
- [ ] Re-trigger Dependabot rescan after each PR merges and confirm the count drops as expected.

### Effort

~1-2 focused sessions per bucket (npm, pip, composer). Could be parallelized across 3 PRs.

### References

- The 12 CVEs addressed by extension in pr/14 (see PR description "What this PR addresses by extension on the Dependabot list").

✂ ─────────────────────────────────────────────────────────────────────

---

## Follow-up #5 — Finish pr/13 in-flight work

✂ ─────────────────────────────────────────────────────────────────────

### Title

Land or shelve pr/13-mechanical-followups in-flight work (44 modified + 25 untracked files)

### Context

When pr/14 branched off `pr/13-mechanical-followups`, the working tree had 44 modified files + 25 untracked files representing work-in-progress (the `OVERNIGHT_2026_06_02.md` overnight run, new migrations 2026_05_29/2026_06_01/2026_06_02/2026_06_03_*, new services `atomic_claim_extractor.py` + `multi_query_expansion.py` + `multi_project_decomposition.py` + `sentence_grounding.py` + `corpus_summarizer.py`, new tests, controller/seeder/handover-doc updates, etc.). pr/14 was carefully written to NOT touch this work — the model-ID sweep in `94808b4` used `git stash push <files>` + `git stash pop` to avoid bundling. This work needs to land on its own or be discarded.

### Acceptance criteria

- [ ] Triage: identify which uncommitted changes belong together as logical PR units (probably ≥ 3 PRs given the mix of migrations + new services + handover docs + controller changes).
- [ ] For each unit: commit, push, open PR against the same base pr/14 cascades into.
- [ ] If any changes are abandoned: `git checkout -- <files>` to drop them cleanly.
- [ ] Confirm `git status` is clean on the pr/13 base before merging pr/14.

### Effort

Depends entirely on what's already done in those files. Surveying the 44 + 25 file changes is itself a half-session.

### References

- `git status` on pr/13-mechanical-followups branch.
- Likely related memories: `project_overnight_run_2026_06_02`, `project_chatgpt_gap_import_2026_06_01`.

✂ ─────────────────────────────────────────────────────────────────────

---

*End of PR14_FOLLOWUPS.md.*
