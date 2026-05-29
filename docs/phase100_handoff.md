## Doc-phase 100 handoff — §11.3 + §11.10 §11 autonomous-safe skeletons

**Status:** Complete. §11 autonomous-safe slice DONE.

## What landed

### §11.3 — restore_workspace Hatchet workflow

`src/fastapi/app/hatchet_workflows/restore_workspace.py`:
- `RestoreWorkspaceInput` (5 fields): workspace_id,
  snapshot_manifest_uri, initiated_by_user_id, restore_request_id,
  dry_run (default true).
- `RestoreWorkspaceOutput` (7 fields): success, stores_restored,
  consistency_check_results, inconsistencies_repaired,
  audit_ledger_entry_id, failure_stage, failure_reason.
- 6h execution_timeout; retries=0.
- Skeleton; docstring documents the v1 restore order (Postgres
  first, then Neo4j/Qdrant/SeaweedFS parallel, Redis last) +
  consistency checks (silver.* ↔ Neo4j edges, chunks in Qdrant,
  bronze URI sanity).

Registered in worker AI pool. `worker --list` now shows 7
long-running workflows.

### §11.10 — audit ledger cold-tier archival

`src/fastapi/app/audit/cold_tier_archive.py`:
- `ArchiveRun` dataclass — rows_archived, cold_tier_uri,
  hot_tier_remaining, verification_passed, failure_reason.
- `archive_window(conn, *, cutoff_before, archive_bucket,
  workspace_id_scope, dry_run)` async function.
- Skeleton; docstring documents the chain-hash-integrity constraint
  (cold-tier manifest carries each row's hash + previous_hash so an
  external auditor can re-walk across hot + cold tiers).

Sits in `app.audit/` alongside `emit_audit` (the writer) and
`hash_chain_proof` (the reader/prover from doc-phase 79). Now
`app.audit/` houses 3 modules: writer, prover, archiver.

## Master-plan §11 progress

| Sub-step | Status |
|---|---|
| 11.0 scope proposal | ✅ |
| 11.1 per-store backup orchestration | pending (ops/infra; Kyle) |
| 11.2 cross-store consistency restore harness | pending |
| 11.3 restore_workspace Hatchet workflow | ✅ skeleton |
| 11.4 5 DR runbooks | pending (ops; Kyle) |
| 11.5 Tenant Isolation Auditor in CI | pending |
| 11.6 single-tenant Helm chart | pending (ops; Kyle) |
| 11.7 self-host K8s manifests | pending (ops; Kyle) |
| 11.8 air-gapped bundle pipeline | pending (heavy ops; Kyle) |
| 11.9 load test harness | pending (ops; Kyle) |
| 11.10 audit ledger cold-tier archival | ✅ skeleton |
| 11.11 acceptance: full DR drill + bundle + load test | pending |

**3 of 12 §11 sub-steps closed (25%).** Per the §11 scope proposal,
the rest is ops/infra work requiring Kyle review. The autonomous-
safe slice (§11.3 + §11.10) is now complete.

## Recommended next tick

Doc-phase 101 = §12.3 + §12.4 + §12.5 (XGBoost training workflow +
inference branch + SHAP writer skeletons). Final autonomous-safe
phase of master plan substrate.

After that: the master plan's autonomous-safe scaffolding pass is
**COMPLETE**. Remaining work requires:
- Kyle SME content (§8.3 Athabasca, §9.3 ontology, §10.2 golden
  questions)
- Image rebuild (geopandas + rasterio + mplstereonet + langgraph +
  weasyprint + python-docx + openpyxl + xgboost + shap)
- Frontend pass (§6.7-6.14, §7.12-7.15, §8.10, §9.12, §10.3/7/11)
- Ops/infra work (§11.1, §11.4-§11.9)

## Carry-overs

Unchanged plus:
- `restore_workspace` dry_run=true must be the default per cold-
  start policy.
- Cold-tier archival destination — Kyle still needs to decide
  SeaweedFS dedicated bucket vs external (Backblaze etc.).
