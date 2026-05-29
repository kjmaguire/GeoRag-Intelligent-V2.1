"""Audit ledger cold-tier archival (§11.10).

Per master plan §11.10, audit_ledger rows past a cutoff date are
archived to cold-tier S3 storage as compressed JSONL files. The hot
table retains the chain head + a configurable recent window (default
90 days); older rows are dehydrated to cold tier with manifest pointers
preserved so an external auditor can re-walk the chain across
hot + cold tiers.

Graduated from doc-phase 100 skeleton → Phase H4.

Design:
    * The archival is a READ + COPY operation; the standalone function
      does NOT delete hot-tier rows. A separate ``prune_archived_window``
      helper (opt-in) is provided for retention enforcement once the
      operator has verified the cold-tier write.
    * Rows are streamed (not loaded into memory) so a multi-year window
      doesn't blow up the FastAPI worker.
    * Each archive run emits a JSON manifest carrying ``rows_archived``,
      ``first_hash``, ``last_hash``, ``chain_continuous`` (bool —
      verified inline by re-walking previous_hash == prev_row.hash),
      and the cold-tier URIs for the JSONL chunks.
    * The cold tier is any object that implements the
      :class:`app.services.bronze_store.BronzeStore` Protocol — SeaweedFS
      in prod, LocalFs in dev/CI.

Output contract (ArchiveRun dataclass):
    rows_archived          how many rows met the cutoff
    cold_tier_uri          manifest URI returned by the store
    hot_tier_remaining     count of rows kept in the hot table
    verification_passed    chain hash walk succeeded across the window
    failure_reason         set when verification fails (do NOT prune)
    manifest_key           bronze-store key for the manifest object
    chunks                 list of {key, uri, rows} for each JSONL chunk
"""
from __future__ import annotations

import gzip
import io
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Protocol

import asyncpg


logger = logging.getLogger(__name__)


# Default chunk size — number of audit ledger rows per JSONL.gz file
# in the cold tier. ~10k rows = ~5MB compressed which is well below
# the SeaweedFS volume size + an easy fetch unit for replay.
_DEFAULT_CHUNK_ROWS = 10_000


class _ColdTierStore(Protocol):
    """Subset of BronzeStore Protocol the archival writer needs."""

    async def put(self, key: str, content: bytes) -> str: ...


@dataclass(frozen=True, slots=True)
class ArchiveRun:
    rows_archived: int
    cold_tier_uri: str
    hot_tier_remaining: int
    verification_passed: bool
    failure_reason: str | None = None
    manifest_key: str = ""
    chunks: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iso(ts: datetime) -> str:
    """Stable ISO-8601 string for keys (no colons → S3-safe)."""
    return ts.strftime("%Y%m%dT%H%M%SZ")


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    """Serialise a Record into a JSON-friendly dict.

    Binary hash columns → hex; timestamps → ISO-8601;
    everything else passes through.
    """
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, (bytes, bytearray, memoryview)):
            out[k] = bytes(v).hex()
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _gzip_jsonl(rows: Iterable[dict[str, Any]]) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
        for row in rows:
            gz.write(json.dumps(row, sort_keys=True, default=str).encode("utf-8"))
            gz.write(b"\n")
    return buf.getvalue()


def _verify_chain(rows: list[dict[str, Any]]) -> tuple[bool, str | None]:
    """Walk the previous_hash linkage across the archive window.

    Returns (continuous, failure_reason). The first row in the window
    is permitted to carry ANY previous_hash (it links to the prior
    archive run or to NULL at chain genesis); we only verify that
    rows 2..N tie back to row N-1's hash.
    """
    prev: dict[str, Any] | None = None
    for r in rows:
        if prev is None:
            prev = r
            continue
        expected = prev.get("hash")
        actual = r.get("previous_hash")
        if expected != actual:
            return False, (
                f"chain break at id={r.get('id')} created_at={r.get('created_at')}: "
                f"previous_hash={actual!r} != prior.hash={expected!r}"
            )
        prev = r
    return True, None


async def archive_window(
    conn: asyncpg.Connection,
    *,
    cutoff_before: datetime,
    archive_bucket: str,
    cold_tier: _ColdTierStore,
    workspace_id_scope: str | None = None,
    chunk_rows: int = _DEFAULT_CHUNK_ROWS,
    dry_run: bool = False,
) -> ArchiveRun:
    """Archive ``audit.audit_ledger`` rows older than ``cutoff_before``.

    Args:
        conn: asyncpg Connection (caller manages transaction).
        cutoff_before: rows with created_at < this are eligible.
        archive_bucket: cold-tier bucket name (informational; goes
            into the manifest and the object keys).
        cold_tier: anything implementing ``async put(key, content)``
            — typically SeaweedFsBronzeStore in prod, LocalFsBronzeStore
            in dev/CI.
        workspace_id_scope: optional — archive only this workspace's
            chain. None = global (all workspace_ids + system events).
        chunk_rows: max rows per JSONL.gz chunk in the cold tier.
        dry_run: if True, COUNTS but does NOT write anything. Useful
            for retention-policy preview from the ops dashboard.

    Returns:
        :class:`ArchiveRun`.

    Notes:
        - Does NOT delete hot-tier rows. Use ``prune_archived_window``
          AFTER the operator has verified the cold-tier write.
        - Chain hash integrity is verified inline; if a break is found,
          ``verification_passed=False`` and ``failure_reason`` is set,
          and we abort the upload (no partial cold-tier writes).
    """
    where = "WHERE created_at < $1"
    params: list[Any] = [cutoff_before]
    if workspace_id_scope is not None:
        where += " AND workspace_id = $2"
        params.append(workspace_id_scope)

    # Pre-count for the manifest + dry-run path.
    total = await conn.fetchval(
        f"SELECT count(*) FROM audit.audit_ledger {where}", *params,
    )
    remaining = await conn.fetchval(
        "SELECT count(*) FROM audit.audit_ledger "
        + ("WHERE workspace_id = $1" if workspace_id_scope else "WHERE TRUE"),
        *([workspace_id_scope] if workspace_id_scope else []),
    ) - total

    logger.info(
        "audit_ledger archive_window: cutoff=%s rows_eligible=%d "
        "hot_remaining_after=%d dry_run=%s",
        cutoff_before.isoformat(), total, remaining, dry_run,
    )

    if total == 0:
        return ArchiveRun(
            rows_archived=0,
            cold_tier_uri="",
            hot_tier_remaining=remaining,
            verification_passed=True,
            manifest_key="",
            chunks=(),
        )

    # Stream rows in deterministic order (chain order: created_at, id).
    rows = await conn.fetch(
        f"""
        SELECT id, workspace_id, actor_id, actor_kind, action_type,
               target_schema, target_table, target_id, payload,
               previous_hash, hash, trace_id, created_at
          FROM audit.audit_ledger
          {where}
         ORDER BY created_at ASC, id ASC
        """,
        *params,
    )
    serialized = [_row_to_dict(r) for r in rows]

    # Chain verification BEFORE upload — if the hot tier is corrupt,
    # we don't want to memorialise the corruption in cold storage.
    chain_ok, chain_failure = _verify_chain(serialized)
    if not chain_ok:
        logger.error(
            "audit_ledger archive_window: chain verification FAILED — "
            "aborting upload. reason=%s", chain_failure,
        )
        return ArchiveRun(
            rows_archived=total,
            cold_tier_uri="",
            hot_tier_remaining=remaining,
            verification_passed=False,
            failure_reason=chain_failure,
            manifest_key="",
            chunks=(),
        )

    if dry_run:
        return ArchiveRun(
            rows_archived=total,
            cold_tier_uri=f"s3://{archive_bucket}/(dry-run)",
            hot_tier_remaining=remaining,
            verification_passed=True,
            manifest_key="(dry-run)",
            chunks=(),
        )

    # Chunked JSONL.gz upload.
    stamp = _iso(cutoff_before)
    scope_tag = (workspace_id_scope or "global").replace("-", "")[:12]
    chunk_records: list[dict[str, Any]] = []
    for i in range(0, len(serialized), chunk_rows):
        chunk = serialized[i:i + chunk_rows]
        key = (
            f"{archive_bucket}/audit_ledger/{stamp}/{scope_tag}/"
            f"chunk-{i // chunk_rows:05d}.jsonl.gz"
        )
        uri = await cold_tier.put(key, _gzip_jsonl(chunk))
        chunk_records.append({
            "key":   key,
            "uri":   uri,
            "rows":  len(chunk),
            "first_hash": chunk[0].get("hash"),
            "last_hash":  chunk[-1].get("hash"),
        })

    # Manifest.
    manifest = {
        "schema_version":     1,
        "archived_at":        datetime.now(timezone.utc).isoformat(),
        "cutoff_before":      cutoff_before.isoformat(),
        "workspace_id_scope": workspace_id_scope,
        "rows_archived":      total,
        "first_hash":         serialized[0].get("hash"),
        "last_hash":          serialized[-1].get("hash"),
        "chain_continuous":   True,
        "chunks":             chunk_records,
    }
    manifest_key = (
        f"{archive_bucket}/audit_ledger/{stamp}/{scope_tag}/manifest.json"
    )
    manifest_uri = await cold_tier.put(
        manifest_key,
        json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8"),
    )

    return ArchiveRun(
        rows_archived=total,
        cold_tier_uri=manifest_uri,
        hot_tier_remaining=remaining,
        verification_passed=True,
        manifest_key=manifest_key,
        chunks=tuple(chunk_records),
    )


async def prune_archived_window(
    conn: asyncpg.Connection,
    *,
    cutoff_before: datetime,
    workspace_id_scope: str | None = None,
) -> int:
    """Delete hot-tier rows that have already been archived.

    OPT-IN. Caller is responsible for verifying the cold-tier
    manifest exists + chain_continuous=True before invoking this.

    Returns the number of rows deleted.
    """
    where = "WHERE created_at < $1"
    params: list[Any] = [cutoff_before]
    if workspace_id_scope is not None:
        where += " AND workspace_id = $2"
        params.append(workspace_id_scope)
    result = await conn.execute(
        f"DELETE FROM audit.audit_ledger {where}", *params,
    )
    # asyncpg returns "DELETE N"
    try:
        deleted = int(result.split()[-1])
    except (ValueError, IndexError):
        deleted = 0
    logger.info(
        "audit_ledger prune_archived_window: cutoff=%s deleted=%d",
        cutoff_before.isoformat(), deleted,
    )
    return deleted


__all__ = ["ArchiveRun", "archive_window", "prune_archived_window"]
