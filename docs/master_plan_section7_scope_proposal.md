# Master-plan §7 (Reporting + dashboards) — Scope Proposal

**Doc-phase 77** — counterpart to §5 and §6 scope proposals.

---

## What §7 ships

"The customer can generate a Technical Due Diligence Report end-to-end —
every section, every citation, every appendix, every signature — and
the result passes the §29.2 export compliance checklist."

Master-plan Phase 7 deliverables (verbatim):
1. Report Builder Graph (§15.1) end-to-end
2. Eleven report types (§15.2) with templates
3. Report package structure (§15.3) including hash chain proof JSON
4. Export Compliance Agent enforcing §29.2 checklist
5. All product-tier dashboards (§16.1) — 8 dashboards
6. Workflow-tier dashboards (§16.2) — 5 dashboards
7. Ops-tier dashboards (§16.3) — 9 dashboards

**Done test:** a Technical Due Diligence Report generates end-to-end
with all sections, all citations, all required appendices, the export
compliance checklist passes, and the Reporting Dashboard shows the run
with full traceability.

---

## Scale assessment

§7 is **the largest phase by deliverable count** so far. Three rough
"sub-phases" emerge naturally:

- **§7-A Report Builder (§15)** — 1 LangGraph + 11 templates + 8
  in-graph agents + 5 output format renderers + 1 Activepieces
  delivery layer. Roughly comparable in size to §3 (PDF stack) and
  §5 (spatial pipeline) combined.
- **§7-B Dashboards (§16)** — 22 dashboards across 3 tiers. Most read
  from `workflow_runs` + product-state tables; Grafana is reused for
  ops tier (§16.3).
- **§7-C Export Compliance Agent (§29.2)** — 10-line checklist; single
  graph node; small but blocking.

---

## Sub-step breakdown estimate

| # | What | Backend | Frontend | Ticks |
|---|---|---|---|---|
| 7.1 | Report Builder Graph skeleton (LangGraph nodes + state) | medium | none | 2 |
| 7.2 | Eleven report-type templates (markdown + JSON manifest) | small | none | 2 |
| 7.3 | Per-section retrieval planner (Evidence Curator Agent) | medium | none | 2 |
| 7.4 | Claim Validator Agent (per-section claim ledger) | medium | none | 1-2 |
| 7.5 | Map/Chart Planner Agent (decides which exhibits each section needs) | medium | none | 1-2 |
| 7.6 | Appendix Builder Agent (manifests + evidence JSON) | medium | none | 1-2 |
| 7.7 | Hash chain proof JSON generator | small | none | 1 |
| 7.8 | Export Compliance Agent (§29.2 checklist) | small | none | 1 |
| 7.9 | PDF/DOCX/XLSX/CSV/JSON renderers (WeasyPrint + python-docx + openpyxl) | medium | none | 2-3 |
| 7.10 | `generate_report` Hatchet workflow | small | none | 1 |
| 7.11 | Activepieces delivery flows (Teams/Slack/email/SharePoint) | medium | none | 2 |
| 7.12 | Reporting Dashboard (product-tier) | small | medium | 1-2 |
| 7.13 | Other 7 product-tier dashboards (§16.1) | small (queries) | medium | 4-6 |
| 7.14 | Workflow-tier dashboards (§16.2) — 5 panels | small | medium | 2-3 |
| 7.15 | Ops-tier dashboards (§16.3) — 9 panels in Grafana | mostly config | none | 2-3 |
| 7.16 | TDD-Report end-to-end acceptance test | mixed | mixed | 1-2 |

**Total: 26-37 ticks.** Largest phase yet. Comparable to §3 (18 ticks)
plus §5 (14-22 ticks) combined.

Frontend skew: ~30% frontend (mostly product + workflow dashboards in
Inertia React + shadcn), ~70% backend (Report Builder Graph, agents,
renderers, Activepieces flows, Grafana configs).

---

## V1.49 / current baseline overlap

What exists today that helps §7:
- **Audit ledger** — hash-chain already implemented (§22 audit_ledger
  table; see `docs/audit_ledger_hash_recipe.md`). §15.3 hash chain proof
  can read from this directly.
- **Citation infra** — every RAG response already returns
  `source_chunk_id`-tagged passages (CLAUDE.md hard rule #4). The
  citation_manifest.csv builder is mostly a join.
- **`workflow_runs` table** — already unified across Hatchet + Dagster +
  Activepieces per §16.5. Most dashboards are queries against this plus
  product-state tables.
- **Grafana stack** — running per docker-compose. §16.3 dashboards are
  JSON-as-code config drops.
- **Hatchet workflows** — pattern established (`ingest_pdf`,
  `re_ocr_page`); `generate_report` follows the same shape.
- **R4/R5 sign-off flow** — partial (§19.6 QP credential verification
  references); needs audit verification at start of §7.

What needs to be built fresh:
- **Report Builder LangGraph** — no precedent in current codebase.
- **All 8 in-graph reporting agents** (Planner, Evidence Curator,
  Conflict Resolver, Claim Validator, Map/Chart Planner, Appendix
  Builder, Presentation Coach, Export Compliance) — skeletons-first
  pattern (doc-phase 49 / 73 / 75).
- **All 11 report-type templates** — markdown + JSON section manifests.
- **PDF/DOCX/XLSX renderers** — `weasyprint`, `python-docx`,
  `openpyxl` dependencies need adding to `pyproject.toml`.
- **22 dashboards** — each = Inertia React page or Grafana JSON.
- **Activepieces delivery flows** — depends on Activepieces being live;
  may overlap with §7 phasing.

---

## Risks

1. **§7 is huge.** 26-37 ticks dwarfs any single phase to date. Real risk
   of "stuck in §7 for 6 weeks." Mitigation: ship in three explicit
   sub-phases (7-A / 7-B / 7-C) and treat each as its own gate.
2. **`workflow_runs` cross-orchestrator unification status unverified.**
   §16.5 makes it a hard requirement. If not in place, §16.2 dashboards
   block until it lands.
3. **WeasyPrint headless-Chrome alternative.** §15.5 lists WeasyPrint OR
   headless Chrome; pick at §7.9 start. WeasyPrint is purely Python and
   avoids needing a Chrome container.
4. **R4/R5 sign-off + QP credential verification (§29.6.1).** Per the
   master plan amendment, this is "staffed-ops work for v1." Means
   the Export Compliance Agent gates exports, but the actual QP
   credential check is a human-in-the-loop step backed by Hatchet
   pause/resume — not a fully automated process.
5. **Eleven report types is a lot.** Some are "automated R3" (digest,
   ingestion quality, GIS sync, what-changed) and reuse the same graph
   with different templates; some are "manual R5" (NI 43-101, CSA
   11-348, Data Room) that require a sign-off flow. Treat the four
   automated R3 types as the §7-A v1 scope; the four manual R5 types
   become §7-A v2 (later in §7 or pushed to a §7.x follow-on).

---

## Dependencies

- **`weasyprint`** — Python PDF renderer. Needs system fonts +
  Pango/Cairo. Image rebuild required.
- **`python-docx`** — DOCX generator. Pure-python; pyproject only.
- **`openpyxl`** — XLSX generator. Pure-python; pyproject only.
- **`langgraph`** — already present (used by Answer Graph in §4 work).
- **Grafana stack** — already running.
- **Hatchet** — already running; `generate_report` workflow follows
  `ingest_pdf` pattern.
- **Activepieces** — verify install status before §7.11. If not yet
  installed, defer Activepieces delivery to §7-A v2 (after §7 closes
  for "report generated + signed but not delivered automatically").

---

## Open questions for Kyle

1. **§7-A sub-phase ordering**: ship "automated R3" report types first
   (Weekly Digest, Ingestion Quality, GIS Sync, What Changed) and gate
   the manual R5 types (TDD, NI 43-101, CSA 11-348, Data Room) behind a
   QP credential verification ceremony? Master plan implies yes but
   doesn't say it explicitly.
2. **WeasyPrint vs headless Chrome** for PDF generation — WeasyPrint
   is simpler (pure Python) but headless Chrome handles more complex
   CSS / chart embeds. Recommend WeasyPrint as default; revisit if
   chart embeds break.
3. **Activepieces install status**: is the Activepieces stack standing
   up yet? §7.11 depends on it.
4. **Phase 8 (Target engine) parallelism**: §7 and §8 share no critical
   path. Target Recommendation Report (§7.2 item 6) lives in §7 but
   depends on §8 outputs. Suggest §8 runs in parallel to §7-B
   (dashboards) once §7-A v1 lands.

---

## Recommendation

§7 is large enough to warrant **three explicit gates**:

- **§7-A v1 (automated reports)** — Report Builder Graph + four
  automated R3 templates + WeasyPrint PDF + Export Compliance Agent.
  Done test: Weekly Project Digest generates end-to-end. ~12-15 ticks.
- **§7-B (dashboards)** — All product-tier + workflow-tier dashboards.
  Ops-tier Grafana panels can ship in parallel. ~6-10 ticks.
- **§7-A v2 (manual reports + delivery)** — TDD + NI 43-101 + CSA +
  Data Room manual templates + R4/R5 sign-off + Activepieces delivery.
  Done test (master plan): TDD generates end-to-end with sign-off.
  ~10-15 ticks.

For the autonomous run continuing through 8am Kyle pickup:
- §7-A v1 first sub-steps (7.1 Report Builder Graph skeleton, 7.2 the
  four automated R3 templates, 7.8 Export Compliance Agent skeleton) are
  all backend-only, skeleton-first work that fits the autonomous pattern.
- Dashboards (§7.12-7.15) should wait for Kyle — Inertia React +
  shadcn product-feel decisions.

---

## TL;DR

§7 = the biggest phase yet (26-37 ticks). Split into 7-A v1 (automated
reports), 7-B (dashboards), 7-A v2 (manual reports + delivery). Backend
substrate (graph + agents + renderers + Hatchet workflow + compliance
agent) is the autonomous-safe slice. Dashboards wait for Kyle.

Autonomous run next ticks: doc-phase 78 = §7.1 (Report Builder Graph
skeleton + Pydantic state model). doc-phase 79 = §7.8 (Export
Compliance Agent skeleton). doc-phase 80 = §7.7 (hash chain proof JSON
generator — small, no callers needed).
