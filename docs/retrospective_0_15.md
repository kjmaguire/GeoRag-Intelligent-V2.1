# GeoRAG Phase 0-15 Retrospective

**Document version:** 1.0
**Status:** Snapshot at Phase 15 close.
**Generated:** During the Phase 16 autonomous overnight run.

This doc captures the trajectory of Phases 0 through 15 in one
readable artifact. For full per-phase context see each
`phase{N}_handoff.md` and its kickoff doc.

---

## 1. Cumulative verifier growth

| Phase close | Verifiers | Total checks | New that phase |
|------------:|----------:|-------------:|---------------:|
| 0 | 6 | 47 | — |
| 1 | 8 | 58 | 11 |
| 2 | 9 | 65 | 7 |
| 3 | 16 | 117 | 52 |
| 4 | 23 | 162 | 45 |
| 5 | 27 | 190 | 28 |
| 6 | 30 | 212 | 22 |
| 7 | 34 | 239 | 27 |
| 8 | 38 | 261 | 22 |
| 9 | 41 | 279 | 18 |
| 10 | 45 | 301 | 22 |
| 11 | 50 | 329 | 28 |
| 12 | 54 | 354 | 25 |
| 13 | 58 | 378 | 24 |
| 14 | 61 | 397 | 19 |
| **15** | **63** | **403** | 11 |

Every phase closed at 100% green on its master sweep.

---

## 2. Phase-by-phase summary

| # | Theme | Key deliverable | Verifier total |
|---|-------|-----------------|---------------:|
| 0 | Infrastructure foundation | Postgres + PostGIS + audit + workspaces + storage tier policy | 47/47 |
| 1 | Hatchet workflow pools | Two-pool worker split (ingestion + ai) + 12 Phase-0 agent workflows + `ingest_pdf` workflow | 11/11 new |
| 2 | Integration edge (Activepieces era) | Activepieces dashboard skeleton (later sunset) | 7/7 new |
| 3 | Kestra migration | Activepieces → Kestra pivot; per-flow JWT auth; HMAC sender verification | 52/52 new |
| 4 | Operational maturation | Per-sender HMAC registry (pgcrypto); Sanctum Kestra SSO proxy; DB-driven flow registry; freshness CI guard; shadow_runs sunset; migration rollup | 45/45 new |
| 5 | Receive-path hardening | Per-sender rate limits; per-flow JWT signing keys; pre-commit hook framework; .env housekeeping; parse_pdf_report OTel spans | 28/28 new |
| 6 | Integration-edge close-out | TracerProvider on worker startup; Caddy edge for Kestra SSO (with WS); multi-kid JWT rotation | 22/22 new |
| 7 | Operational close-out | Dagster tracer bootstrap; flow_jwt_keys reaper; TLS on Caddy edge; rollup filename rationalisation | 27/27 new |
| 8 | Phase 7 close-out + HA scoping | Dagster image rebuild; admin UI for JWT keys; Caddy TLS issuer parametrization; Hatchet HA design doc | 22/22 new |
| 9 | Ops follow-throughs | Dagster Tempo e2e probe; rotate-with-overlap button; Caddy ACME wiring scaffold | 18/18 new |
| 10 | Ops close-out + scoping pivot | JWT rotation audit row; rate-limit verifier flake fix; sender register UI; **Phase 11 scoping inventory** (caught that RAG framework was already implemented) | 22/22 new |
| 11 | RAG validation + discipline | Section 04i audit doc; golden-test baseline (2/35); prompts/ subdirectory bootstrap; golden smoke in master sweep; pre-commit hook end-to-end activation | 28/28 new |
| 12 | RAG discipline + ops UI | Init.py docstring drift fix; first inline prompt migration (rephrase_system); Layer 6 constraints externalised to JSON; sender HMAC rotate + rotation history panel | 25/25 new |
| 13 | Golden fixture seed | classifier_system prompt migration; **PLS-* fixture seeded** (10 collars under TEST_PROJECT_ID); peak unlock observed (13/35) | 24/24 new |
| 14 | R-P13-1 root cause + ops | agent_system prompt migration (third, 4133 chars); HMAC rotation overlap window; **R-P13-1 root-caused** to stale `silver.mv_collar_summary` MV — fixed in fixture migration | 19/19 new |
| 15 | Nightly MV refresh + audit | `mv_refresh_silver` Hatchet workflow on cron `0 3 * * *`; orchestrator inline-prompt audit (R-P15-1 scoped) | 11/11 new |

---

## 3. Architectural shifts caught mid-run

Three significant pivots happened during the run, each documented
in the relevant phase's handoff:

1. **Activepieces → Kestra (Phase 3).** User redirected mid-Phase-3
   from Activepieces to Kestra as the integration edge. Phase 3 +
   Phase 3 Step 7 sunset removed Activepieces; phase2_step{1..5}
   verifiers were archived as obsolete during the Phase 4 sweep.

2. **RAG framework already exists (Phase 10).** The Phase 10 Step
   4 Explore-agent inventory caught that the agent code was 30
   files (orchestrator.py at 5184 lines, all six §04i hallucination
   layers implemented) — reframed Phase 11 from "build RAG" to
   "validate the RAG framework that's already there."

3. **R-P13-1 root cause (Phase 14).** Phase 13's "intermittent
   refusal" turned out to be `silver.mv_collar_summary` drifting
   to empty between cold/warm runs. Phase 14 Step 3 root-caused
   it without a code investigation prompt — the diagnosis fell
   out of reading the orchestrator's `_build_project_facts`
   helper.

---

## 4. Things that work end-to-end today

| Capability | Verified by |
|------------|-------------|
| Hatchet workflows in two named pools (5 ingestion + 12 AI) | phase1_step2 |
| FastAPI integration trigger with per-flow Bearer JWT auth | phase3_step3 |
| External notification HMAC verification + rate limit + audit ledger | phase3_step5 + phase5_step1 |
| Per-flow JWT signing keys with multi-kid overlap rotation | phase5_step2 + phase6_step3 |
| Caddy edge HTTPS at `:8443` with `forward_auth` to Laravel/Sanctum | phase7_step3 + phase6_step2 |
| Dagster image with OTel SDK exporting parse spans to Tempo | phase8_step1 + phase9_step1 |
| Admin UI for: flow flags, senders (register/rotate/disable), per-flow JWT keys (with rotate), rotation history | phase4_step5 + phase8_step2 + phase10_step3 + phase12_step4 + phase14_step2 |
| Pre-commit hooks (pydantic-freshness + system-prompt-version-bump) | phase11_step5 |
| Section 04i hallucination defence (6 layers) | phase11_section_04i_audit.md |
| Nightly maintenance crons (audit-verify, mv_refresh, flow_jwt_key_reaper) | phase7_step2 + phase15_step1 |
| Golden-test fixture (10 PLS-* collars + project) | phase13_step3 |
| Layer 6 constraints in SME-editable JSON config | phase12_step3 |
| 4 inline prompts migrated to canonical `app/agent/prompts/` tree | phase12_step2 + phase13_step1 + phase14_step1 |

---

## 5. Known carry-overs into Phase 16+

Top of the queue (highest leverage):

1. **R-P14-3** — golden-test pass-rate investigation. Peak
   observed: 13/35. Floor: 2/35. The MV-refresh fix lifted the
   peak but didn't make pass count reliable run-to-run.
2. **R-P15-1** — bundled orchestrator prompt migration (10
   variants). Scoping doc landed Phase 15 Step 2.
3. **R-P11-baseline-2** — public-geoscience golden fixture seed
   (3 pgeo tests). Needs `public_geoscience.*` schema content.
4. **R-P11-B** — frontend Search/Query page. First user-facing
   RAG surface.
5. **R-P11-l4-fixture** — CI fixture for Layer 4 entity grounding
   (Neo4j entities + cross-references).

Deferred indefinitely (waiting for SME or forcing function):
R-P3-5 (dual-write harness), R-P3-6 (Hatchet HA),
R-P3-9 (vendor profiles), R-P9-2 (production ACME),
R-P12-l6-sme-review (Kyle review of Layer 6 constraints).

---

End of retrospective.
