# COVERAGE.md

> Derived from `HANDOVER_MANIFEST.md` §1–§25 with owner column from
> `PASS_1_MAPPING.md` §3. Loop terminator — every manifest line ticked.

**Legend:** `[x]` covered · `[ ]` uncovered (none after PASS 2 completion).

---

## Coverage matrix

| Manifest § | Item | Owner | Status | Landed at |
|---|---|---|---|---|
| 1 | FastAPI routes (109 endpoints) | API | [x] | API §5.2 |
| 1a | Router-prefix map | API | [x] | API §5.1 |
| 1b | main.py include_router overrides | API | [x] | API §5.1 |
| 2 | Pydantic AI agents (42, @georag_agent) | SAD | [x] | SAD §3.3.1 |
| 2-split | Worker pool that hosts each agent | CICD | [x] | CICD §6.3 + SAD §3.3.1 cross-ref |
| 2a | Non-decorated agent-tier modules (~50 in `app/agent/*.py`) | SAD | [x] | SAD §3.3.4 |
| 2b | Prompt registry + system/model routing (20 prompt files) | SAD | [x] | SAD §3.3.3 |
| 2c | ML / fine-tune lifecycle (target_scoring_ml, reranker, bge-small, ADRs 0008/0011) | SAD | [x] | SAD §3.3.5 |
| 2d | Eval harness + golden-query gate (`services/eval/`, `eval.*` tables) | SAD | [x] | SAD §3.3.6 |
| 2e | Hallucination 6-layer responsibility table + failure modes | SAD | [x] | SAD §4.1 |
| 2e-i | Layer 6 constraint values (7 rules × ranges) | SAD | [x] | SAD §4.1 |
| 2f | Refusal spine staging tiers (SHADOW → TERMINAL → LOWCOST → FULL) | SAD | [x] | SAD §4.1 |
| 2g | Model-routing tier system (FAST/STANDARD/DEEP) + select_tier rules | SAD | [x] | SAD §3.3.3 |
| 2h | Reranker identity = bge-reranker-base + per-class top-K table | SAD | [x] | SAD §2.1 + §3.3.5 |
| 2i | RRF fusion formula + per-list weighting via bm25_weight | DFS | [x] | DFS §2.3.1 |
| 7a-roles | `georag_app` + `martin_ro` provisioning gap (cold-start blocker) | DFS | [x] | DFS §4.1.1 + INDEX §5.3 |
| 3 | LangGraph subgraphs (3) + 8 intents | SAD | [x] | SAD §3.3.2 |
| 3-split | RAG flow names the agentic_retrieval graph | DFS | [x] | DFS §2.3 |
| 3a | Per-graph node inventory (agentic_retrieval / report_builder / target_recommendation) | SAD | [x] | SAD §3.3.2 |
| 4 | Hatchet workflow modules (52; live verified 2026-06-02; +3 June: `embed_pending_passages_smoke`, `ingest_zip_archive`, `qdrant_payload_audit`) — data flows | DFS | [x] | DFS §2.2 + DFS §7.2 + DFS §7.4 + SAD §3.4 |
| 4-split | Hatchet engine compose envelope + WORKER_POOL | CICD | [x] | CICD §6.3 |
| 4a | Worker pool `ingestion` | CICD | [x] | CICD §6.3 |
| 4b | Worker pool `ai` | CICD | [x] | CICD §6.3 |
| 4c | Hatchet cron schedules (30 declarations) | CICD | [x] | CICD §6.7 |
| 5 | Dagster assets (56 top-level + 5 bronze_to_silver + 7 silver_to_gold; live verified 2026-05-29) | DFS | [x] | DFS §2.1 |
| 5a | Dagster schedules + sensor | CICD | [x] | CICD §6.5 |
| 5b | Dagster asset checks (27 / 6 files) | CICD | [x] | CICD §6.8 |
| 5b-split | DQ checks referenced from silver section | DFS | [x] | DFS §2.1 |
| 6 | Kestra flows (3) — data flows | DFS | [x] | DFS §6.1 |
| 6-split | external_notification HMAC envelope | API | [x] | API §8.2 |
| 7a | PG tables (248 / 17 schemas; manifest §7a stale at 174/15) | DFS | [x] | DFS §4.2 + §4.2a bronze + §4.2b silver + §4.2c gold |
| 7b | PG functions (23) | DFS | [x] | DFS §4.4 |
| 7b-split | Martin MVT functions named in Tile API section | API | [x] | API §7.1 + §7.2 |
| 7c | PG triggers (7) | DFS | [x] | DFS §4.5 |
| 7d | PG materialized views (1) | DFS | [x] | DFS §4.6 |
| 7e | PG extensions (15) | DFS | [x] | DFS §4.3 |
| 7f | Neo4j labels (10) + relationship types (12) | DFS | [x] | DFS §4.7 |
| 7g | Qdrant collections (9) + payload indices | DFS | [x] | DFS §4.8 |
| 7h | Redis logical DBs (4) | DFS | [x] | DFS §4.9 |
| 7i | ClickHouse (Langfuse trace store) | DFS | [x] | DFS §4.10 |
| 7j | SeaweedFS buckets (3) + Laravel disks | DFS | [x] | DFS §5.1 |
| 8a | Reverb channels (30 patterns) | API | [x] | API §6.1 |
| 8b | Reverb event classes (11) + payload field shapes | API | [x] | API §6.2 + §6.2.1 |
| 2j | Retrieval-quality overhaul 2026-06-02 (5 new services + 6 new flags + GeoRAGResponse.grounding_report) | SAD | [x] | SAD §3.3.7 + DFS §2.3 |
| 5c | Dagster + Hatchet 2026-06 deltas (smoke + zip-archive + qdrant-audit workflows) | DFS | [x] | DFS §2.2 + SAD §3.4 |
| 24a | 1500 ChatGPT gap-question import + 2 new question_set buckets | DFS | [x] | DFS §4.2 (eval row) + SAD §3.3.6 |
| 9a | Laravel api.php (61 direct + 6 resource = 67 entries, ~91 expanded; live verified 2026-05-29) | API | [x] | API §3.1–§3.9 |
| 9b | Laravel web.php (155 routes; live verified 2026-05-29) | API | [x] | API §3.10 + §3.11 |
| 9b-split | /admin/integrations/kestra/{path?} SSO bridge | CICD | [x] | CICD §6.9 |
| 10 | Martin MVT function inventory | API | [x] | API §7.1 + §7.2 |
| 11 | CI/CD workflow files (7) | CICD | [x] | CICD §1 |
| 12 | Dockerfiles (5) | CICD | [x] | CICD §4.1 |
| 13a | Compose services (33) | SAD | [x] | SAD §2.3 + §2.2 topology |
| 13a-split | Per-service Dockerfile build context | CICD | [x] | CICD §4.1 |
| 13a-split-2 | Storage services hosting persistent data | DFS | [x] | DFS §4.1 |
| 13b | Compose overlays (5) | SAD | [x] | SAD §2.3 |
| 13c | Compose named volumes (23) | DFS | [x] | DFS §5.2 |
| 13d | Compose `georag` bridge network | SAD | [x] | SAD §2.3 |
| 14 | Laravel config files (20) | SAD | [x] | SAD §4.4 |
| 15 | Env surface (.env, FastAPI Settings, flags) | SAD | [x] | SAD §4.4 |
| 15-split | Secrets / SOPS provisioning + preflight | CICD | [x] | CICD §6.1 |
| 16a | scripts/operator/ | CICD | [x] | CICD §6.11 |
| 16b | ops/setup/ | CICD | [x] | CICD §6.11 |
| 16c | ops/runbooks/ (38) | INDEX | [x] | INDEX §3.4 |
| 16c-split | Specific runbooks referenced in CD procedures | CICD | [x] | CICD §6.11 |
| 16d | ops/baselines/ | CICD | [x] | CICD §6.12 |
| 16e | ops/audit/ | CICD | [x] | CICD §6.12 |
| 17a | Prometheus scrape jobs (12) | SAD | [x] | SAD §4.3 |
| 17b | Prometheus alert defs (64 / 13 files) | SAD | [x] | SAD §4.3 |
| 17b-split | Alertmanager receiver routing | CICD | [x] | CICD §6.9 |
| 17c | Grafana dashboards (14 + 3 product) | SAD | [x] | SAD §4.3 |
| 17d | Loki / Tempo / OTel / Promtail | SAD | [x] | SAD §4.3 |
| 18 | ADRs (12) | INDEX | [x] | INDEX §3.2 |
| 18-split | ADR titles named in SAD Key Decisions | SAD | [x] | SAD §5.1 |
| 19 | Eloquent models | SAD | [x] | SAD §3.2 |
| 20 | Inertia pages | SAD | [x] | SAD §3.1 + §3.10 |
| 21 | Laravel controllers | API | [x] | API §3 + §4 (handler column) |
| 22 | Cross-service boundaries | SAD | [x] | SAD §2.2 topology diagram |
| 23a | docs/architecture/ existing files | INDEX | [x] | INDEX §3.5 |
| 23b | docs/ top-level files | INDEX | [x] | INDEX §3.3 |
| 23c | docs/adr/ pointer | INDEX | [x] | INDEX §3.2 |
| 23d | docs/runbooks/ + ops/runbooks/ pointer | INDEX | [x] | INDEX §3.4 |
| 23e | other docs subdirs | INDEX | [x] | INDEX §3.6 |
| 24 | Confirmation-ledger harvest (43 + 9 PASS-2 items) | INDEX | [x] | INDEX §5.1–§5.9 |
| 25 | Inventory totals table | INDEX | [x] | INDEX §4 |

---

## Verification gates

- [x] No orphan `§NsM`-style subsections (`grep -E '§[0-9]+s[0-9]+'` = 0 hits)
- [x] No duplicate top-level section numbers within any single doc
- [x] Section numbers monotonic within each doc (verified per `## N.` grep)
- [x] Zero matches for `pass [0-9]` across all 5 docs
- [x] Zero matches for `net-new` across all 5 docs
- [x] Zero matches for `earlier pass` across all 5 docs
- [x] Zero matches for `doc-phase` across all 5 docs
- [x] Every coverage row above is `[x]`
- [x] No item silently moved between docs without manifest update

---

## Final section-ordering snapshot

**HANDOVER_INDEX.md** — 6 top-level sections: 1. The four documents · 2. Reading order · 3. Canonical sources · 4. Inventory totals · 5. Needs Confirmation · 6. Operator handoff. ✅

**SAD.md** — 6 top-level sections: 1. System overview · 2. Architecture summary + topology · 3. Component architecture · 4. Cross-cutting concerns · 5. Key decisions · 6. Needs Confirmation. ✅ (Collapsed prior §6s1–§6s55 wall into §4.)

**DFS.md** — 8 top-level sections: 1. Primary data domains · 2. End-to-end data flows · 3. Data classification · 4. Persistence + database architecture · 5. Storage / cache / pub-sub · 6. Outbound integrations + exports · 7. Reliability · 8. Needs Confirmation. ✅ (Two prior `§4` sections merged.)

**API_DOCUMENTATION.md** — 10 top-level sections: 1. Surfaces at a glance · 2. Authentication · 3. /api/v1/* · 4. /internal/* · 5. FastAPI · 6. Reverb · 7. Tile API · 8. Contracts · 9. Versioning · 10. Needs Confirmation. ✅ (Fixed §7→§8→§7a→§8a→§9 ordering.)

**CICD_PIPELINE.md** — 8 top-level sections: 1. Workflow inventory · 2. CI pipeline · 3. CD pipeline · 4. Container build · 5. Test posture · 6. Operations · 7. Local dev · 8. Needs Confirmation. ✅ (Fixed duplicate §4, interleaved §3a/§3b, §6a0/§6a.)

---

*PASS 2 complete. Loop terminated.*
