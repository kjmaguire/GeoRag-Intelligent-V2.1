## Doc-phase 140 handoff — §25.4 support_packet agent

**Status:** Live + 5/5 pytest cases + 6 production support packets assembled. **83/83 substrate verifier**.

## What landed

Third of 5 §25.4 support agents. Assembles a complete, exportable
support packet for one ticket — ticket info + all triage/investigation
audit anchors + related decisions + trace links. Engineering hand-off
artifact.

### New live service — `app/services/support_cockpit/support_packet.py`

~200 lines. Pure async. Exports:
- `build_support_packet(ticket_id, pool=None)` — end-to-end assembly
- `SupportPacket` NamedTuple (structured result)

Assembly pipeline:
1. Load ticket (all 13 ops.support_tickets columns)
2. Pull every `support.ticket.triaged` audit anchor for the ticket
3. Pull every `support.ticket.investigated` audit anchor for the ticket
4. Pull all `ops.support_ticket_traces` rows for the ticket
5. Pull recent `silver.decision_records` for the workspace (last 7d, limit 25)
6. Compose human-readable summary
7. Emit `support.packet.assembled` audit anchor containing packet metadata
8. Return `SupportPacket` NamedTuple

### Real (not synthetic) graduation

Unlike doc-phase 132/134/136/137/138/139 which graduated with
synthetic-stub evaluators, **doc-phase 140 is fully real** — packet
assembly is deterministic data aggregation, no LLM/heuristic synthesis
required. Future enhancement adds Langfuse trace embeds + replay URIs
but the existing assembly is content-true.

## Tests — `src/fastapi/tests/test_support_packet.py`

**5 pytest cases, all green:**

| Test | Verifies |
|---|---|
| `test_build_support_packet_includes_ticket_info` | Bare ticket info + audit anchor emitted |
| `test_build_support_packet_includes_triage_chain` | Triage + investigation anchors visible in packet |
| `test_build_support_packet_unknown_id_raises` | ValueError on bad UUID |
| `test_build_support_packet_summary_format` | Summary string mentions category + severity + counts |
| `test_build_support_packet_multiple_calls_emit_distinct_anchors` | Each assembly gets a fresh anchor (snapshots, not deduped) |

## Live verification — 6 production packets

Assembled support packets for all 6 production tickets:

```text
c625f3ee... packet=0de56745 triage=1 invest=1 traces=1  (wrong_answer)
b0ace1df... packet=1e5e5618 triage=1 invest=1 traces=1  (failed_report)
5e74c8a0... packet=9ec59831 triage=1 invest=1 traces=1  (failed_ingestion)
2671467c... packet=05175217 triage=1 invest=1 traces=1  (performance)
9037c265... packet=e90a53b7 triage=1 invest=1 traces=1  (integration_issue)
459d8fcc... packet=f3bfcc7f triage=1 invest=1 traces=1  (other)
```

Each ticket now has the full `triaged → investigated → packet_assembled`
audit chain landed. Engineering can pull any of the packet anchor ids
and get the complete diagnostic context for that ticket.

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest tests/test_support_packet.py -v
# → 5 passed in 0.75s

bash scripts/autonomous_run_substrate_verify.sh
# → 83/83 checks passed
```

## Cumulative session state

- **Doc-phase ticks this run:** 140
- **§25.4 support agents graduated:** **3 of 5** (ticket_triage,
  root_cause_investigation, support_packet)
- **§18.2 nodes graduated:** 6 of 12
- **§15.1 nodes graduated:** 4 of 12
- **§21.3 capture hooks wired:** 1 of 8
- **Hatchet workflow skeletons graduated:** 1 of 11
- **Reasoning agent skeletons graduated:** 1 (hypothesis_generator)
- **Live pytest cases:** 130 (125 + 5)
- **Substrate verifier:** **83/83 PASS**

## What's next

- **Doc-phase 141** — §15.1 + §18.2 LangGraph wiring (thread the
  graduated nodes into actual Pregel pipelines so generate_report +
  score_targets Hatchet workflows can be graduated end-to-end)
- **Doc-phase 142** — §25.4 customer_response_drafting agent
- **Doc-phase 143** — §25.4 escalation_routing agent (completes the
  §25.4 5-agent set)

## Carry-overs

- Packet assembly is a snapshot. Repeat calls emit distinct anchors
  (intentional — each assembly captures state at that moment for
  later replay).
- The packet's `evidence_json_uri` / `pdf_uri` / etc. fields aren't
  populated here — that's §7.9 export-package territory.
- A future enhancement would add Langfuse trace_url embeds for each
  investigation_anchor entry (using doc-phase 118's
  `open_trace_with_audit` helper).
