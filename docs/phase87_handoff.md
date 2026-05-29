## Doc-phase 87 handoff — §8.5 + §8.12 — eleven target agent skeletons

**Status:** Complete. All 11 agents import + callable.

## What landed

New package `app/agents/phase8/` with 11 §18.4 agent files plus
`__init__.py`:

| Agent | File | Risk | §18.4 role |
|---|---|---|---|
| Deposit Model | `deposit_model.py` | R1 | Loads deposit model + active version |
| Evidence Layer | `evidence_layer.py` | R1 | Per-factor evidence assembly |
| Candidate Generation | `candidate_generation.py` | R2 | Writes candidate_zones polygons |
| Target Scoring | `target_scoring.py` | R2 | Writes scores + factor breakdown |
| Uncertainty | `uncertainty.py` | R2 | Writes uncertainty rows |
| Constraint | `constraint.py` | R1 | Applies exclusion polygons |
| Recommendation Explainer | `recommendation_explainer.py` | R2 | "Never say drill here" enforcement |
| Geologist Sign-Off | `geologist_signoff.py` | **R5** | QP credential + audit ledger |
| Backtesting | `backtesting.py` | R2 | Phase 12 input — performance metrics |
| Field Outcome | `field_outcome.py` | R2 | Phase 12 training signal |
| Scenario Planning | `scenario_planning.py` | R1 | "What if" simulation |

Each agent:
- `@georag_agent`-decorated with appropriate risk_tier
- Signature + output contract documented; body raises NotImplementedError
- Docstring includes the §18.4 role + special enforcement notes

### Notable risk-tier choices

- **R5** for `geologist_signoff` — highest tier in the system. Per
  `wrapper.py:195-198`, R5 idempotency requires (workspace_id,
  target_id, signoff_session_id) on ctx. `target_id` kwarg name
  matches the wrapper requirement.
- **R2** for `recommendation_explainer` — drafts language for the R5
  sign-off; treated as a writer because it produces the rationale that
  geologists then sign.
- **R1** for `scenario_planning` — purely simulation; no production
  writes.

### Smoke test

    docker exec georag-fastapi python -c "
        from app.agents import phase8
        agents = [getattr(phase8, n) for n in phase8.__all__]
        print(len(agents))  # 11
        print(all(callable(a) for a in agents))  # True
    "

## Master-plan §8 progress

| Sub-step | Status |
|---|---|
| 8.0 scope proposal | ✅ |
| 8.1 `targeting.*` schema | ✅ |
| 8.2 Deposit model loader | pending |
| 8.3 Athabasca uranium SME content | pending (Kyle) |
| 8.4 Target Recommendation Graph | ✅ state + node stubs |
| 8.5 11 target agents skeletons | ✅ DONE |
| 8.6 score_targets Hatchet workflow | pending |
| 8.7 Weighted scoring formula | pending (Kyle weights) |
| 8.8 SHAP-equivalent score factor writer | pending |
| 8.9 Sign-off ceremony glue | pending (Kyle decisions) |
| 8.10 Target Pack map layer | pending |
| 8.11 Activepieces target workflows | pending |
| 8.12 Recommendation Explainer Agent | ✅ (folded into §8.5 batch) |
| 8.13 Acceptance test | pending |

**4 of 14 §8 sub-steps closed** (§8.0, §8.1, §8.4, §8.5 + §8.12
co-landed). All R1-R5 agents now skeleton-callable. The §8-A backbone
parallels §7-A v1's state at doc-phase 81.

## Recommended next tick

Doc-phase 88 = §8.6 `score_targets` Hatchet workflow skeleton. Pattern
matches `generate_report` (doc-phase 83). Wires the target graph into
a durable workflow with R5 pause-resume + audit emission. Register in
the AI pool.

After that: §8.2 deposit model loader skeleton + 10 empty template
stubs (no SME content yet; populate when Kyle provides Athabasca
uranium data).

## Carry-overs

1. **Unified image rebuild** — same blockers as §5/§7.
2. **Kyle SME data** for §8.3 Athabasca + §8.7 scoring weights.
3. **Activepieces install** — gates §8.11 + §7.11.
