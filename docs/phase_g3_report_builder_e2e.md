# Phase G.3 — `generate_report` end-to-end: all 12 nodes graduated

**Status:** Complete. Master-plan §7 "Report Builder Graph end-to-end"
deliverable closes for the minimum-viable surface; the 11 report
templates were already shipped in doc-phase 137 + the workflow
wrapper graduated in 145. This phase fills the remaining 8 skeleton
nodes so a report invocation produces a real bundle.

## What graduated (8 of 12 nodes)

Previously raised `NotImplementedError`. Each now ships a
minimum-viable body that produces real artifacts:

| Node | Phase G.3 body | Deferred to follow-up |
|---|---|---|
| `generate_section_drafts` | Deterministic per-section markdown assembly: one Claim per evidence item, body bullets reference source_chunk_ids | Per-section LLM narration |
| `validate_claims` | Marks `.validated=True/False` based on evidence presence + visibility tag; records reject count in `compliance_checks` | §04i numeric / entity guards over LLM-drafted claim text |
| `attach_citations` | Injects `[DATA:N]` markers into each claim's text + bullet; populates `state.citation_payload` with marker → source map | Cross-store claim_ledger join + Trust Inspector wiring |
| `generate_maps_charts` | Catalogs requested `map_kinds` + `chart_kinds` per section; stamps placeholder check on compliance_checks | MapLibre static-tile + Plotly export pipeline (§17.4) |
| `build_appendix` | Builds evidence.json + citation_manifest.json + source_manifest.json as inline `data:application/json;base64` URIs + a SHA-256-hashed proof signature | SeaweedFS upload + audit-ledger hash-chain join |
| `compliance_check` | Runs a 5-gate export checklist (evidence presence, citations populated, claim validation rate, risk tier valid, manifests built); sets `state.compliance_passed` + `failure_reason` on miss | §29.2 full 10-gate checklist (jurisdictions, licensing, PII) |
| `geologist_approval` | R3 → auto-approve; R4 → pending geologist SignOffRecord; R5 → pending geologist + QP | Hatchet pause-resume + sign-off UI |
| `export_package` | Renders a single self-contained markdown bundle (concatenated section bodies + citation footer + provenance proof footer) as a `data:text/markdown;base64` URI | WeasyPrint PDF + python-docx + openpyxl renderers |
| `activepieces_delivery` | Log-only; sets `delivery_dispatched=True` (renamed in spirit to Kestra per ADR-0001 but function name preserved for backward-compat) | Real Kestra flow dispatch (§7.11) |

## Graph wiring extension

`src/fastapi/app/services/report_builder/graph.py` now wires all 12
nodes in the documented §15.1 order. The pipeline list is exposed as
a module-level `_PIPELINE` constant so future tests can assert the
shape without re-instantiating LangGraph.

Each node checks `state.failure_reason` at entry — once an earlier
node fails, downstream nodes pass state through unchanged so the
caller can inspect partial state without crashing.

## Output shape

A successful R3 run now produces a `ReportBuilderState` with:

* `pdf_uri` — `data:text/markdown;base64,...` of the full report
* `evidence_json_uri`, `citation_manifest_uri`, `source_manifest_uri` —
  inline JSON manifests
* `hash_chain_proof` — `{evidence_sha256, evidence_item_count, citation_count}`
* `compliance_passed = True`, `sign_off_complete = True` (R3),
  `delivery_dispatched = True`

R4/R5 runs produce all of the above *and* a `sign_offs` list with one
`SignOffRecord(role="geologist")` (R4) or two records (R5), each with
`signed_at = None`. The actual sign-off arrives via a future Hatchet
pause/resume + admin UI tick.

## Test coverage

`src/fastapi/tests/test_report_builder_e2e.py` — **10 tests**:

* R3 end-to-end runs all 12 nodes + produces a markdown bundle with
  a citations section and a provenance-proof footer
* R4 records a pending geologist sign-off; no QP
* R5 records both geologist + QP pending sign-offs
* R3 auto-completes sign-off with empty `sign_offs`
* Exported markdown contains `## Citations` header even when no claims
* `evidence_json_uri` decodes to a valid JSON document
* Compliance fails when no evidence is present (records `failure_reason`)
* Compliance passes when a single evidence item is present + injects
  the `[DATA:1]` marker into the bundle
* Invalid evidence (empty `source_chunk_id`) marks claim unvalidated
* Pipeline short-circuits cleanly when an earlier node sets `failure_reason`

Canary suite post-G.3: **229 / 0** (+10, no regressions since G.2).

## Worker registration

`generate_report` was already registered in doc-phase 145; no
`worker.py` change needed.

## What this enables

* A real markdown bundle ships from any of the 11 report templates,
  end-to-end, in <1s wall-time on the dev workstation.
* The master plan §7 "Done when" — "a Technical Due Diligence Report
  generates end-to-end with all sections, all citations, all required
  appendices, and the export compliance checklist passes" — is now
  achievable: TDR is an R4 report, ships through all 12 nodes, fails
  the compliance check only when its evidence threshold isn't met
  (correct behaviour).
* The Trust Inspector + admin "report queue" pages already exist in
  the frontend and can now display real `data:` URIs from a workflow
  run.

## Carry-overs

1. **LLM-driven section drafting** (`generate_section_drafts`) — the
   current body is deterministic. Plug in a per-section LLM call that
   takes the evidence items + section template and produces narrative
   text. Section template's `evidence_query` field is the natural
   handle for the prompt.
2. **MapLibre static tile rendering** (`generate_maps_charts`) — the
   §17.4 follow-up. Today only the request manifest ships.
3. **WeasyPrint + python-docx renderers** (`export_package`) —
   `pyproject.toml` already declares the deps. Add per-format
   renderers under `app/services/report_builder/renderers/`.
4. **Hatchet pause/resume for R4/R5 sign-off** — sign_offs are
   recorded as pending; the actual workflow needs to suspend and
   wait on a `/admin/reports/<id>/sign-off` POST that fills
   `SignOffRecord.signed_at` + records the audit anchor.
5. **Real Kestra delivery dispatch** — the `activepieces_delivery`
   stub now logs only. Wire the actual Kestra flow registration +
   the per-target dispatcher (email, Teams, SharePoint, Slack).
6. **SeaweedFS upload** — replace the inline `data:` URIs with
   per-report SeaweedFS object paths once the bucket convention firms
   up in Phase 11.

## Files added / changed

* **`src/fastapi/app/services/report_builder/nodes.py`** — 8 node
  bodies graduated (~280 LOC of new code replacing the
  `NotImplementedError` stubs)
* **`src/fastapi/app/services/report_builder/graph.py`** — all 12
  nodes wired; ordered pipeline exposed as `_PIPELINE` constant
* **`src/fastapi/tests/test_report_builder_e2e.py`** — new, 10 tests
