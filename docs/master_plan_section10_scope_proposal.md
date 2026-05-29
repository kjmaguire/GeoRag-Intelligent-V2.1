# Master-plan §10 (Eval harness + Customer Support Cockpit) — Scope Proposal

**Doc-phase 95** — sixth scope proposal in the §5→§6→§7→§8→§9→§10
sequence.

---

## What §10 ships

"Operational maturity. Without these the system can ship features
but cannot evolve safely or support customers at scale."

Master-plan Phase 10 deliverables (verbatim):
1. Golden questions schema (§24.1) populated with first 100 questions
   across all question sets
2. Authoring workflow UI in Laravel admin
3. Hatchet `evaluate_workspace` workflow
4. Regression thresholds (§24.4) enforced; promotion blocking active
5. Eval Dashboard
6. Customer Support Cockpit (§25) deployed
7. Support agents (§25.4)
8. Workflow replay capability (`support_replay` Hatchet workflow)

**Done test:** a candidate prompt change triggers eval, the eval
blocks promotion on a regression, Kyle can fix or override with
logged rationale; a customer-reported issue ticket traces through
the cockpit to root cause and replays safely.

---

## Two natural sub-phases

**§10-A — Eval harness:**
- Golden questions schema (one new table)
- 100 questions across ~8 question sets (50/50 SME + autonomous —
  some questions are mechanical OCR triage questions)
- `evaluate_workspace` Hatchet workflow
- Regression thresholds + promotion blocking
- Eval Dashboard

**§10-B — Customer Support Cockpit:**
- 3 `ops.*` tables (support_tickets, support_ticket_traces,
  support_replay_runs)
- 5 support agents (Ticket Triage, Root Cause Investigation, Support
  Packet, Customer Response Drafting, Escalation Routing)
- `support_replay` Hatchet workflow
- Laravel admin module under `/admin/support/*`

§10-A unblocks safe iteration; §10-B unblocks safe customer support
at scale. Both are operational rather than product-facing — almost
no work waits on Kyle SME beyond golden-question authoring.

---

## Sub-step breakdown estimate

| # | What | Backend | Frontend | SME | Ticks |
|---|---|---|---|---|---|
| 10.1 | `eval.golden_questions` schema | small | none | none | 1 |
| 10.2 | Golden questions seed loader (8 question_set slots) | small | none | small (per-set sample questions) | 1 |
| 10.3 | Question authoring UI in Laravel admin | small | medium | small | 2-3 |
| 10.4 | `evaluate_workspace` Hatchet workflow + per-question fan-out | medium | none | none | 2 |
| 10.5 | Eval result schema (`eval.run_results`, per-question pass/fail) | small | none | none | 1 |
| 10.6 | Regression threshold config + promotion-gate enforcer | medium | none | none | 1-2 |
| 10.7 | Eval Dashboard (Laravel admin Inertia React) | small (API) | medium | none | 2 |
| 10.8 | `ops.support_tickets` + `support_ticket_traces` + `support_replay_runs` schema | small | none | none | 1 |
| 10.9 | 5 support agents skeletons | small | none | none | 1-2 |
| 10.10 | `support_replay` Hatchet workflow | medium | none | none | 1-2 |
| 10.11 | Customer Support Cockpit UI (Laravel admin module) | small (API) | medium | none | 3-4 |
| 10.12 | Cross-workspace access audit emission | small | none | none | 1 |
| 10.13 | LangFuse trace replay link integration | small | small | none | 1 |
| 10.14 | Acceptance test: golden eval blocks regression + ticket trace + replay | mixed | mixed | small | 1-2 |

**Total: 19-26 ticks.** Comparable to §6, smaller than §7/§8.

Frontend skew: ~35% (authoring UI, eval dashboard, cockpit). Backend
is the bigger half.

---

## V1.49 / current baseline overlap

What exists:
- **`workflow_runs` table** — eval + replay both inspect this.
- **`audit_ledger`** — cockpit reads time-windowed excerpts directly.
- **LangFuse** — already running per `docs/langfuse-langgraph-tooling-setup.md`.
- **Hatchet workflow pattern** — `evaluate_workspace` + `support_replay`
  follow the established mold.
- **`@georag_agent` decorator** — 5 support agent skeletons plug in
  directly.

What's new:
- `eval.*` schema (new schema namespace; 2-3 tables).
- `ops.*` schema (new schema namespace; 3 tables).
- Question authoring + eval dashboard + support cockpit — three new
  Laravel admin modules.
- Golden-question content — first 100 questions across 8 question
  sets.
- Regression threshold tuning (the "what's an acceptable drop"
  conversation).

---

## Risks

1. **Golden-question content (§10.2 / Phase 10 deliverable #1)**
   needs 100 questions across 8 sets. Most are mechanical (OCR
   triage, schema mapping), but the core_chat + public_private_boundary
   + target_recommendation sets need SME. Estimate: 50% autonomous-
   safe, 50% Kyle/SME.
2. **Regression threshold tuning** — first eval runs may "fail" on
   thresholds that turn out to be too strict. Mitigation: ship with
   warning-only mode for first 2 weeks; flip to blocking after
   threshold curves stabilize.
3. **Cross-workspace access in cockpit** — sensitive. The audit
   emission (§10.12) is the mitigation; needs Kyle review before
   §10-B ships externally.
4. **Workflow replay safety** — replaying ingestion workflows could
   re-fire OCR + LLM calls (cost). Dry-run mode (§25.1) is the
   contract; the `support_replay` workflow MUST honor it.

---

## Open questions for Kyle

1. **Golden-question ownership** — who authors? 50% mechanical sets
   can land via autonomous-safe content; the SME-dependent sets need
   Kyle or external contractor.
2. **Regression threshold mode** — start in warning-only or jump
   straight to blocking? Suggest warning-only for 2 weeks then
   blocking.
3. **Cockpit access model** — `ops` role only, or also support-
   contractor read-only role?
4. **Replay cost ceiling** — should `support_replay` enforce a
   workspace-monthly cost ceiling for replays? Avoids
   accidentally-expensive replay storms.

---

## Recommendation

§10 autonomous-safe slice:
- **§10.1** + **§10.5** schema migrations (eval.golden_questions +
  eval.run_results)
- **§10.2** golden-question seed loader skeleton (8 empty question_set
  slots; mechanical sets can populate via doc-phase 97 follow-up)
- **§10.4** + **§10.6** Hatchet `evaluate_workspace` workflow +
  threshold-gate logic skeleton
- **§10.8** ops.* schema (3 tables)
- **§10.9** 5 support agent skeletons
- **§10.10** `support_replay` Hatchet workflow skeleton

That gets §10 to roughly the same scaffold state as §9 at doc-phase
93 — backbone scaffolded, behavior pending. Frontend (§10.3, §10.7,
§10.11) waits for Kyle.

---

## TL;DR

§10 = operational maturity. 19-26 ticks. Mostly backend + new schemas
+ new agent skeletons + Hatchet workflows. Frontend (~35%) waits for
Kyle. SME content for ~50 golden questions is the only material
content blocker.

Autonomous run next ticks: doc-phase 96 = §10.1 + §10.5 schemas.
Doc-phase 97 = §10.4 evaluate_workspace workflow. Doc-phase 98 =
§10.9 5 support agent skeletons + §10.10 support_replay workflow.
Doc-phase 99 = §11 (DR + perf hardening) scope proposal.
