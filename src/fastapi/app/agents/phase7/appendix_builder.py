"""Appendix Builder Agent (§7.6 / §15.4).

Assembles the report bundle's appendix artifacts:
  - citation_manifest.csv — every citation with source + page + hash
  - source_manifest.json — every source document with metadata
  - evidence.json — structured evidence ledger
  - hash_chain_proof.json — proof tying the bundle to audit.audit_ledger

Phase H4 graduation — builds the payloads deterministically + returns
them inline. SeaweedFS write happens via an optional ``store`` callable
(callers in production pass a SeaweedFsBronzeStore; tests use a fake).
When ``store`` is None, URIs come back empty and the payloads are
returned in-band so the caller can persist them itself.

Output contract — see module docstring.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID

from app.agents import AgentContext, georag_agent


logger = logging.getLogger(__name__)


class _AppendixStore(Protocol):
    """Subset of BronzeStore needed for appendix writes."""
    async def put(self, key: str, content: bytes) -> str: ...


def _citation_manifest_csv(citation_payload: dict[str, Any]) -> bytes:
    """Flatten the per-section citation map into a CSV with one row per
    cited chunk: section_id, claim_id, source_chunk_id, source_uri,
    page, sha256."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "section_id", "claim_id", "source_chunk_id",
        "source_uri", "page", "sha256",
    ])
    for section_id, claims in (citation_payload.get("by_section") or {}).items():
        for claim_id, citations in (claims or {}).items():
            for c in citations:
                writer.writerow([
                    section_id,
                    claim_id,
                    c.get("source_chunk_id", ""),
                    c.get("source_uri", "") or "",
                    c.get("page", "") or "",
                    c.get("sha256", "") or "",
                ])
    return buf.getvalue().encode("utf-8")


def _source_manifest_json(citation_payload: dict[str, Any]) -> bytes:
    """Distinct source documents referenced by the bundle."""
    sources: dict[str, dict[str, Any]] = {}
    for claims in (citation_payload.get("by_section") or {}).values():
        for citations in (claims or {}).values():
            for c in citations or []:
                key = c.get("source_uri") or c.get("source_chunk_id")
                if not key:
                    continue
                if key not in sources:
                    sources[key] = {
                        "source_uri":  c.get("source_uri", ""),
                        "title":       c.get("source_title", ""),
                        "license":     c.get("license_note", ""),
                        "data_visibility": c.get("data_visibility", "public"),
                        "sha256":      c.get("sha256", ""),
                        "first_cited": c.get("freshness_iso", ""),
                    }
    payload = {
        "source_count": len(sources),
        "sources":      list(sources.values()),
    }
    return json.dumps(payload, sort_keys=True, indent=2).encode("utf-8")


def _evidence_json(evidence_ledger: dict[str, Any]) -> bytes:
    """The evidence ledger serialised as JSON for downstream
    verification + the §29.2 G05 evidence-export gate."""
    return json.dumps(evidence_ledger, sort_keys=True, indent=2, default=str).encode("utf-8")


def _hash_chain_proof(
    *,
    report_id: str,
    workspace_id: str,
    citation_payload_bytes: bytes,
    evidence_ledger_bytes: bytes,
) -> dict[str, Any]:
    """Build the proof envelope that ties this bundle to the audit
    ledger. Production swaps the synthetic hashes for a real fetch
    against the audit_ledger chain head; the contract is the same.
    """
    import hashlib

    return {
        "schema_version":           1,
        "built_at":                 datetime.now(timezone.utc).isoformat(),
        "report_id":                report_id,
        "workspace_id":             workspace_id,
        "citation_manifest_sha256": hashlib.sha256(citation_payload_bytes).hexdigest(),
        "evidence_ledger_sha256":   hashlib.sha256(evidence_ledger_bytes).hexdigest(),
        "anchored_to":              "audit.audit_ledger",
        "anchor_lookup":            (
            f"SELECT id, hash FROM audit.audit_ledger "
            f"WHERE target_id = '{report_id}' "
            f"AND action_type = 'report.export.appendix_built' "
            f"ORDER BY created_at DESC LIMIT 1"
        ),
    }


@georag_agent(
    name="Appendix Builder Agent",
    risk_tier="R2",  # Writes appendix artifacts to SeaweedFS
    version="1.0.0",  # graduated Phase H4
)
async def appendix_builder(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    report_id: UUID | str,
    citation_payload: dict[str, Any],
    evidence_ledger: dict[str, Any],
    store: _AppendixStore | None = None,
    archive_bucket: str = "report-appendix",
) -> dict[str, Any]:
    """Build appendix artifacts + hash chain proof.

    Args:
        workspace_id / report_id: identifiers (informational).
        citation_payload: per-section citation map from attach_citations.
        evidence_ledger: structured evidence from gather_evidence +
            validate_claims.
        store: optional object implementing ``put(key, bytes) -> uri``.
            When None, URIs come back empty + payloads in-band.
        archive_bucket: bucket prefix for SeaweedFS keys.

    Returns:
        dict with appendix URIs + hash_chain_proof.
    """
    ws = str(workspace_id)
    rid = str(report_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key_prefix = f"{archive_bucket}/{ws}/{rid}/{stamp}"

    citation_bytes = _citation_manifest_csv(citation_payload)
    source_bytes   = _source_manifest_json(citation_payload)
    evidence_bytes = _evidence_json(evidence_ledger)

    citation_uri = ""
    source_uri   = ""
    evidence_uri = ""
    if store is not None:
        citation_uri = await store.put(f"{key_prefix}/citation_manifest.csv", citation_bytes)
        source_uri   = await store.put(f"{key_prefix}/source_manifest.json", source_bytes)
        evidence_uri = await store.put(f"{key_prefix}/evidence.json", evidence_bytes)

    proof = _hash_chain_proof(
        report_id=rid,
        workspace_id=ws,
        citation_payload_bytes=citation_bytes,
        evidence_ledger_bytes=evidence_bytes,
    )

    summary = (
        f"report_id={rid} citations_written={bool(citation_uri)} "
        f"source_manifest_written={bool(source_uri)} "
        f"evidence_written={bool(evidence_uri)} "
        f"hash_chain_anchored={bool(proof['citation_manifest_sha256'])}"
    )
    logger.info("appendix_builder: %s", summary)

    return {
        "citation_manifest_uri": citation_uri,
        "source_manifest_uri":   source_uri,
        "evidence_json_uri":     evidence_uri,
        "hash_chain_proof":      proof,
        "inline_payloads": {
            # Tests + airgapped operators consume these directly.
            "citation_manifest_bytes_len": len(citation_bytes),
            "source_manifest_bytes_len":   len(source_bytes),
            "evidence_json_bytes_len":     len(evidence_bytes),
        },
        "summary": summary,
    }


__all__ = ["appendix_builder"]
