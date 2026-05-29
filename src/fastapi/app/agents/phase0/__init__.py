"""Phase 0 agent implementations.

Each agent is a thin module that:
  - decorates a single async function with ``@georag_agent``
  - reads from the relevant Postgres schemas
  - writes findings to silver.store_reconciliation_findings or
    silver.corpus_health_findings as appropriate
  - emits an audit_ledger entry via the wrapper

These are Phase 0 skeletons — production-grade implementations land in
Phase 1+ when Qdrant + Neo4j are in scope and the agents gain cross-store
checks. The skeletons exercise the operational contract, prove the audit +
findings tables work end-to-end, and give Phase 6 something to invoke.

The full registry-spec behaviour (sample sizes, alerting cadence, p95 SLAs)
is intentionally relaxed for Phase 0 — see kickoff §Step 6 build cards for
the per-agent narrowing rationale.

Roster (10 of 11 Phase 0 agents — GPU/VRAM Health is implemented as
Prometheus rules, not a Python agent):

  R0:
    - Tenant Isolation Auditor      tenant_isolation_audit
    - Lineage Reporter Agent        lineage_walk
    - Index Health Agent            index_health_check
    - Store Reconciliation Agent    store_reconciliation_run
    - Model Upgrade Watch Agent     model_upgrade_watch_run
    - vLLM Security Check Agent     vllm_security_check_run
    - Model Cost Summary Agent      model_cost_summary_run
    - LLM Incident Diagnosis Agent  llm_incident_diagnosis_run

  R2:
    - Storage Tiering Agent         storage_tiering_run
    - Support Packet Agent          support_packet_assemble
"""

from .graph_tenant_auditor import graph_tenant_audit
from .index_health import index_health_check
from .lineage_reporter import lineage_walk
from .llm_incident_diagnosis import llm_incident_diagnosis_run
from .model_cost_summary import model_cost_summary_run
from .model_upgrade_watch import model_upgrade_watch_run
from .storage_tiering import storage_tiering_run
from .store_reconciliation import store_reconciliation_run
from .support_packet import support_packet_assemble
from .tenant_isolation_auditor import tenant_isolation_audit
from .vllm_security_check import vllm_security_check_run

__all__ = [
    "tenant_isolation_audit",
    "graph_tenant_audit",
    "lineage_walk",
    "index_health_check",
    "store_reconciliation_run",
    "storage_tiering_run",
    "model_upgrade_watch_run",
    "vllm_security_check_run",
    "model_cost_summary_run",
    "llm_incident_diagnosis_run",
    "support_packet_assemble",
]
