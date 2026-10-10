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
    * Rows are streamed through a server-side cursor, twice, inside one
      REPEATABLE READ snapshot: the first pass verifies the hash chains and
      holds one hash per chain; the second pass uploads, holding one chunk.
      Memory is bounded by ``chunk_rows``, not by the size of the window, and
      nothing is uploaded unless the whole window verified.
    * The ledger is not one chain but one chain PER ``workspace_id`` (NULL is
      the system chain), each ordered by ``(created_at, id)`` -- the order the
      BEFORE INSERT trigger links them in. Verification walks each chain on
      its own; walking the window as one sequence reported a break as soon as
      two workspaces had rows in it.
    * A run archives ``[cutoff_after, cutoff_before)``. ``cutoff_after`` is the
      watermark the previous completed run left (the workflow reads it from
      that run's audit anchor), so a night archives the night's rows, not the
      whole history again. When it is set, each chain's first row must also
      continue from the newest row of that chain before the window.
    * Each archive run emits a JSON manifest carrying ``rows_archived``,
      ``first_hash``, ``last_hash``, ``chain_continuous`` (bool -- verified
      by re-walking previous_hash == prev_row.hash PER CHAIN), ``chain_heads``
      (the last hash of every chain, which the next window continues from),
      and the cold-tier URIs for the JSONL chunks.
    * The cold tier is any object with the bronze-store put/get shape
      (georag_object_storage.protocols) -- S3 in prod, local in dev/CI.

Output contract (ArchiveRun dataclass):
    rows_archived          how many rows met the window
    cold_tier_uri          manifest URI returned by the store
    hot_tier_remaining     count of rows kept in the hot table
    verification_passed    chain hash walk succeeded across the window
    failure_reason         set when verification fails (do NOT prune)
    manifest_key           bronze-store key for the manifest object
    chunks                 list of {key, uri, rows} for each JSONL chunk
    chain_heads            {workspace_id | "system": last hash} of the window
"""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import logging
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import asyncpg

logger = logging.getLogger(__name__)


# Default chunk size — number of audit ledger rows per JSONL.gz file
# in the cold tier. ~10k rows = ~5MB compressed which is well below
# the SeaweedFS volume size + an easy fetch unit for replay.
_DEFAULT_CHUNK_ROWS = 10_000

#: Name of the system chain (workspace_id NULL) in ``chain_heads``.
SYSTEM_CHAIN = "system"

_ROW_COLUMNS = (
    "id, workspace_id, actor_id, actor_kind, action_type, "
    "target_schema, target_table, target_id, payload, "
    "previous_hash, hash, trace_id, created_at"
)


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
    chain_heads: dict[str, str | None] = field(default_factory=dict)

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


def _chain_name(workspace_id: Any) -> str:
    return SYSTEM_CHAIN if workspace_id is None else str(workspace_id)


class _ChainWalk:
    """Verify previous_hash linkage one row at a time, one chain at a time.

    Rows arrive in ``(created_at, id)`` order across all workspaces, so the
    rows of any one chain arrive in that chain's own order. ``heads`` holds
    the last hash seen per chain: the only state, whatever the window's size.
    """

    def __init__(self) -> None:
        self.heads: dict[Any, str | None] = {}
        self.first_hash: str | None = None
        self.last_hash: str | None = None
        self.rows = 0

    def knows(self, workspace_id: Any) -> bool:
        return workspace_id in self.heads

    def feed(self, row: dict[str, Any], *, seed: str | None = None) -> str | None:
        """Check one row; return a failure reason, or None.

        ``seed`` is the hash of the chain's newest row before the window. It
        applies only to the chain's first row in the window; when it is None
        that row may carry any previous_hash (it links to the prior archive
        run, to rows already pruned, or to NULL at chain genesis).
        """
        chain = row.get("workspace_id")
        actual = row.get("previous_hash")
        if chain in self.heads:
            expected, what = self.heads[chain], "prior.hash"
        elif seed is not None:
            expected, what = seed, "the chain's newest row before the window"
        else:
            expected, what = actual, ""
        failure = None
        if expected != actual:
            failure = (
                f"chain break in {_chain_name(chain)} at id={row.get('id')} "
                f"created_at={row.get('created_at')}: previous_hash={actual!r} != {what}={expected!r}"
            )
        self.heads[chain] = row.get("hash")
        if self.first_hash is None:
            self.first_hash = row.get("hash")
        self.last_hash = row.get("hash")
        self.rows += 1
        return failure

    def chain_heads(self) -> dict[str, str | None]:
        return {_chain_name(c): h for c, h in self.heads.items()}


def _verify_chain(
    rows: list[dict[str, Any]],
    seeds: Mapping[Any, str | None] | None = None,
) -> tuple[bool, str | None]:
    """Walk the previous_hash linkage across the archive window, per chain.

    Returns (continuous, failure_reason). ``rows`` are in ``(created_at, id)``
    order across workspaces; each workspace_id (NULL included) is its own
    chain, so rows 2..N of every chain must tie back to the row before them IN
    THAT CHAIN. The first row of a chain is permitted to carry ANY previous_hash
    (it links to the prior archive run or to NULL at chain genesis) unless
    ``seeds`` names the hash it must continue from.
    """
    walk = _ChainWalk()
    for r in rows:
        seed = seeds.get(r.get("workspace_id")) if seeds and not walk.knows(r.get("workspace_id")) else None
        failure = walk.feed(r, seed=seed)
        if failure is not None:
            return False, failure
    return True, None


def _window(
    cutoff_before: datetime,
    cutoff_after: datetime | None,
    workspace_id_scope: str | None,
) -> tuple[str, list[Any]]:
    where = "WHERE created_at < $1"
    params: list[Any] = [cutoff_before]
    if workspace_id_scope is not None:
        params.append(workspace_id_scope)
        where += f" AND workspace_id = ${len(params)}"
    if cutoff_after is not None:
        params.append(cutoff_after)
        where += f" AND created_at >= ${len(params)}"
    return where, params


@contextlib.asynccontextmanager
async def _snapshot(conn: asyncpg.Connection) -> AsyncIterator[None]:
    """One consistent view of the ledger for both passes over the window.

    A cursor needs a transaction anyway. REPEATABLE READ keeps the verify pass
    and the upload pass looking at the same rows. A caller that already holds
    a transaction keeps its own (asyncpg refuses a different isolation level
    nested inside it).
    """
    if conn.is_in_transaction():
        yield
        return
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        yield


async def _chain_seed(
    conn: asyncpg.Connection, workspace_id: Any, window_start: datetime,
) -> str | None:
    """Hash of the chain's newest row before the window, hex, or None.

    Two branches rather than ``IS NOT DISTINCT FROM`` so each is a probe on
    audit_ledger_workspace_id_idx (workspace_id, created_at DESC); the same
    ordering the BEFORE INSERT trigger uses to pick a row's parent.
    """
    if workspace_id is None:
        value = await conn.fetchval(
            "SELECT hash FROM audit.audit_ledger WHERE workspace_id IS NULL "
            "AND created_at < $1 ORDER BY created_at DESC, id DESC LIMIT 1",
            window_start,
        )
    else:
        value = await conn.fetchval(
            "SELECT hash FROM audit.audit_ledger WHERE workspace_id = $1 "
            "AND created_at < $2 ORDER BY created_at DESC, id DESC LIMIT 1",
            workspace_id, window_start,
        )
    return None if value is None else bytes(value).hex()


async def _verify_window(
    conn: asyncpg.Connection,
    sql: str,
    params: list[Any],
    *,
    window_start: datetime | None,
    prefetch: int,
) -> tuple[_ChainWalk, str | None]:
    """Pass 1: stream the window through the chain walk. Stops at the first break."""
    walk = _ChainWalk()
    async for record in conn.cursor(sql, *params, prefetch=prefetch):
        row = _row_to_dict(record)
        chain = row.get("workspace_id")
        seed = None
        if window_start is not None and not walk.knows(chain):
            seed = await _chain_seed(conn, chain, window_start)
        failure = walk.feed(row, seed=seed)
        if failure is not None:
            return walk, failure
    return walk, None


async def archive_window(
    conn: asyncpg.Connection,
    *,
    cutoff_before: datetime,
    archive_bucket: str,
    cold_tier: _ColdTierStore,
    workspace_id_scope: str | None = None,
    chunk_rows: int = _DEFAULT_CHUNK_ROWS,
    dry_run: bool = False,
    cutoff_after: datetime | None = None,
) -> ArchiveRun:
    """Archive ``audit.audit_ledger`` rows in ``[cutoff_after, cutoff_before)``.

    Args:
        conn: asyncpg Connection. Outside a transaction the function opens its
            own REPEATABLE READ READ ONLY one around the two passes; inside one
            it uses the caller's.
        cutoff_before: rows with created_at < this are eligible.
        archive_bucket: cold-tier bucket name (informational; goes
            into the manifest and the object keys).
        cold_tier: anything implementing ``async put(key, content)``
            — typically SeaweedFsBronzeStore in prod, LocalFsBronzeStore
            in dev/CI.
        workspace_id_scope: optional — archive only this workspace's
            chain. None = global (all workspace_ids + system events).
        chunk_rows: max rows per JSONL.gz chunk in the cold tier.
        dry_run: if True, COUNTS and verifies but does NOT write anything.
            Useful for retention-policy preview from the ops dashboard.
        cutoff_after: the watermark -- rows with created_at < this were
            archived by an earlier run and are skipped. None = from the
            beginning of the ledger.

    Returns:
        :class:`ArchiveRun`.

    Notes:
        - Does NOT delete hot-tier rows. Use ``prune_archived_window``
          AFTER the operator has verified the cold-tier write.
        - Chain hash integrity is verified per workspace chain before
          anything is uploaded; if a break is found,
          ``verification_passed=False`` and ``failure_reason`` is set,
          and we abort the upload (no partial cold-tier writes).
    """
    where, params = _window(cutoff_before, cutoff_after, workspace_id_scope)

    # Pre-count for the manifest + dry-run path.
    total = await conn.fetchval(
        f"SELECT count(*) FROM audit.audit_ledger {where}", *params,
    )
    # What stays hot: everything from the cutoff on. (Not "all rows minus this
    # window": with a watermark, rows before it are archived but still here.)
    remaining_params: list[Any] = [cutoff_before]
    remaining_where = "WHERE created_at >= $1"
    if workspace_id_scope is not None:
        remaining_params.append(workspace_id_scope)
        remaining_where += " AND workspace_id = $2"
    remaining = await conn.fetchval(
        f"SELECT count(*) FROM audit.audit_ledger {remaining_where}", *remaining_params,
    )

    logger.info(
        "audit_ledger archive_window: window=[%s, %s) rows_eligible=%d "
        "hot_remaining_after=%d dry_run=%s",
        cutoff_after.isoformat() if cutoff_after else "-inf",
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

    # Rows in deterministic order (chain order: created_at, id), streamed.
    sql = f"SELECT {_ROW_COLUMNS} FROM audit.audit_ledger {where} ORDER BY created_at ASC, id ASC"
    prefetch = max(1, min(chunk_rows, 5_000))

    async with _snapshot(conn):
        # Chain verification BEFORE upload — if the hot tier is corrupt,
        # we don't want to memorialise the corruption in cold storage.
        walk, chain_failure = await _verify_window(
            conn, sql, params, window_start=cutoff_after, prefetch=prefetch,
        )
        if chain_failure is not None:
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
                chain_heads=walk.chain_heads(),
            )

        # Pass 2: chunked JSONL.gz upload, one chunk in memory at a time.
        stamp = _iso(cutoff_before)
        scope_tag = (workspace_id_scope or "global").replace("-", "")[:12]
        chunk_records: list[dict[str, Any]] = []
        chunk: list[dict[str, Any]] = []
        uploaded = 0

        async def _flush() -> None:
            nonlocal chunk, uploaded
            if not chunk:
                return
            key = (
                f"{archive_bucket}/audit_ledger/{stamp}/{scope_tag}/"
                f"chunk-{len(chunk_records):05d}.jsonl.gz"
            )
            uri = await cold_tier.put(key, _gzip_jsonl(chunk))
            chunk_records.append({
                "key":   key,
                "uri":   uri,
                "rows":  len(chunk),
                "first_hash": chunk[0].get("hash"),
                "last_hash":  chunk[-1].get("hash"),
            })
            uploaded += len(chunk)
            chunk = []

        async for record in conn.cursor(sql, *params, prefetch=prefetch):
            chunk.append(_row_to_dict(record))
            if len(chunk) >= chunk_rows:
                await _flush()
        await _flush()

    if uploaded != walk.rows:
        # Only possible if the caller's own transaction was READ COMMITTED and
        # the ledger changed between the passes. The verified rows are not the
        # uploaded rows, so do not publish a manifest that says they are.
        reason = (
            f"the ledger changed during the archive: verified {walk.rows} rows, "
            f"uploaded {uploaded}; no manifest written"
        )
        logger.error("audit_ledger archive_window: %s", reason)
        return ArchiveRun(
            rows_archived=total,
            cold_tier_uri="",
            hot_tier_remaining=remaining,
            verification_passed=False,
            failure_reason=reason,
            manifest_key="",
            chunks=tuple(chunk_records),
        )

    # Manifest.
    chain_heads = walk.chain_heads()
    manifest = {
        "schema_version":     2,
        "archived_at":        datetime.now(UTC).isoformat(),
        "window_start":       cutoff_after.isoformat() if cutoff_after else None,
        "cutoff_before":      cutoff_before.isoformat(),
        "workspace_id_scope": workspace_id_scope,
        "rows_archived":      uploaded,
        # First/last of the window in (created_at, id) order across chains.
        # Per-chain continuity is what chain_continuous attests; chain_heads is
        # what the next window has to continue from.
        "first_hash":         walk.first_hash,
        "last_hash":          walk.last_hash,
        "chain_continuous":   True,
        "chain_heads":        chain_heads,
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
        rows_archived=uploaded,
        cold_tier_uri=manifest_uri,
        hot_tier_remaining=remaining,
        verification_passed=True,
        manifest_key=manifest_key,
        chunks=tuple(chunk_records),
        chain_heads=chain_heads,
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

    No workflow, route or script calls this today. Since
    2026_10_10_100200_make_audit_ledger_append_only the ledger refuses every
    UPDATE and DELETE (privilege revoked for ``georag_app``; a BEFORE UPDATE OR
    DELETE trigger for every other role), so this raises unless the caller is a
    superuser that has run ``SET LOCAL session_replication_role = replica`` in
    the same transaction. That is deliberate: pruning the tamper-evident ledger
    is a reviewed operator act, not something a service role does. Partition
    retention (pg_partman DROP) is DDL and is unaffected.

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


__all__ = ["ArchiveRun", "SYSTEM_CHAIN", "archive_window", "prune_archived_window"]
