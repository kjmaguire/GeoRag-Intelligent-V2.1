"""Phase 1 Step 4 — Hatchet ``ingest_pdf`` workflow (Step 4C refactor).

Shadow-replacement of the v1.49 PDF ingestion path documented in
``docs/phase1_v149_ingest_pdf_survey.md``. Decomposed into 3 steps that
mirror the v1.49 contract:

    1. preflight    — S3 GET, magic bytes, sha256, page count, size cap
    2. parse        — calls app.services.ingest.pdf_report.parse_pdf_report()
                      which is the canonical v1.49 entry point — runs the
                      full pipeline (fitz → Cohere Parse/Tesseract OCR
                      routing, OCR if scanned, metadata, sections,
                      resource tables)
    3. persist      — writes silver.reports + silver.shadow_runs + audit

Step 4A originally decomposed parse into 5 sub-steps; that was unnecessary
since the v1.49 contract IS the monolithic ``parse_pdf_report()``. The
3-step shape mirrors v1.49 exactly so the diff contract has cleanly
comparable outputs. Per-stage observability comes from the parser's own
logging + future Phase 11 instrumentation.

Pool: ``ingestion``. Action: ``ingest_pdf``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import hashlib
import json
import logging
import os
import time
import uuid
from concurrent.futures.process import BrokenProcessPool
from typing import Any
from uuid import UUID

import asyncpg
from georag_object_storage import (
    Bucket,
    StorageConfig,
    get_async_storage_client,
    get_storage_client,
)
from hatchet_sdk import (
    ConcurrencyExpression,
    ConcurrencyLimitStrategy,
    Context,
    NonRetryableException,
)
from pydantic import BaseModel, Field

from app.agent.workspace_context import LEGACY_DEFAULT_TENANT_UUID
from app.audit import emit_audit
from app.db import bind_workspace_scope
from app.db.dsn import build_dsn
from app.hatchet_workflows import _progress as ingest_progress
from app.hatchet_workflows import hatchet
from app.metrics import WORKSPACE_RESOLUTION_FAILURES
from app.services.ingest.upload_limits import human_bytes, max_upload_bytes

log = logging.getLogger("georag.hatchet.ingest_pdf")


# Subprocess pool used to run the (CPU-heavy, GIL-holding) PDF parse work
# off the main asyncio loop. With `asyncio.to_thread`, parses like
# pdfplumber+OCR on 500-page PDFs hold the GIL so long that Hatchet's
# heartbeat handler (4s deadline) can't fire and the worker gets marked
# dead, in-flight tasks get cancelled, and we lose progress. A subprocess
# pool gives each parse its own GIL → main loop stays responsive.
#
# Phase 5 (2026-05-22) — subprocess pool sizing + memory guard.
#
# Pool sizing:
#   PARSE_SUBPROCESS_MAX_WORKERS env var, default min(os.cpu_count(), 4).
#   Previously hardcoded to 1 — meant every Hatchet slot beyond the first
#   queued behind one running parse. With 4 workers, a 12-PDF batch
#   completes ~3-4× faster (matches available core count without
#   oversubscribing the GPU pipeline).
#
# Memory guard:
#   Before submitting a parse to the pool, the parse task awaits
#   _wait_for_memory_headroom() which polls psutil.virtual_memory().available.
#   If RAM < PARSE_MIN_FREE_RAM_MB (default: 30% of the container limit, see
#   _default_min_free_mb), the task waits up to
#   PARSE_MEMORY_WAIT_MAX_S (default 120) then raises MemoryError so
#   Hatchet retries on a freer worker.
_PARSE_POOL: Any = None


def _compute_parse_max_workers() -> int:
    """Resolve the subprocess pool size from env + system characteristics.

    Order of precedence:
      1. PARSE_SUBPROCESS_MAX_WORKERS env var (if a positive integer)
      2. min(os.cpu_count() or 1, 4) — safe default that scales with the
         host but caps at 4 (peak ~6 GB combined RSS for 4 parallel parses,
         leaves headroom for the AI worker + vLLM + system).
      3. Falls back to 1 if psutil is unavailable so the pool still works
         in stripped-down environments.
    """
    env = os.environ.get("PARSE_SUBPROCESS_MAX_WORKERS")
    if env:
        try:
            v = int(env)
            return max(1, v)
        except ValueError:
            log.warning(
                "ingest_pdf: PARSE_SUBPROCESS_MAX_WORKERS=%r is not an int; "
                "using computed default",
                env,
            )
    try:
        import psutil  # noqa: F401, PLC0415
    except ImportError:
        log.warning(
            "ingest_pdf: psutil unavailable — memory guard disabled, pool "
            "size locked to 1 (legacy behavior)"
        )
        return 1
    return max(1, min(os.cpu_count() or 1, 4))


#: cgroup v2 puts the container's own limit and usage here. An ECS Fargate
#: task is a cgroup-limited container on a shared host, so this is the file
#: that describes the 8 GiB the worker actually has.
_CGROUP_V2_MAX = "/sys/fs/cgroup/memory.max"
_CGROUP_V2_CURRENT = "/sys/fs/cgroup/memory.current"

#: cgroup v1 fallback, for older hosts.
_CGROUP_V1_LIMIT = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
_CGROUP_V1_USAGE = "/sys/fs/cgroup/memory/memory.usage_in_bytes"

#: Per-cgroup counters, used to take reclaimable page cache out of "usage".
_CGROUP_V2_STAT = "/sys/fs/cgroup/memory.stat"
_CGROUP_V1_STAT = "/sys/fs/cgroup/memory/memory.stat"


def _read_int(path: str) -> int | None:
    try:
        with open(path) as fh:  # noqa: PTH123
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _read_stat_bytes(path: str, key: str) -> int:
    """One counter from a cgroup ``memory.stat`` file; 0 when unreadable."""
    try:
        with open(path) as fh:  # noqa: PTH123
            for line in fh:
                name, _, value = line.partition(" ")
                if name == key:
                    return int(value.strip())
    except (OSError, ValueError):
        # No stat file (not in a cgroup) or an unparsable line: treat the
        # counter as 0, i.e. fall back to the raw usage figure.
        log.debug("cgroup memory.stat unreadable: %s (%s)", path, key, exc_info=True)
        return 0
    return 0


def _cgroup_available_mb() -> float | None:
    """Memory left inside THIS container's cgroup, or None if unreadable.

    psutil.virtual_memory() reads /proc/meminfo, which inside a
    containerised host reports the HOST, not the cgroup limit. The worker is
    4 vCPU / 8 GiB (default threshold ~2.4 GB); with
    two 400-page parses already running and the container at 7 GiB of its 8,
    psutil would report the host's free memory — many GB on a shared
    host — the guard would clear instantly, a third parse would be submitted,
    and the cgroup OOM-killer would fire. Which is the exact failure the
    guard was added to prevent, and the symptom the 2026-08-17 comment below
    chased and attributed to leaked worker processes.
    """
    for limit_path, usage_path, stat_path, cache_key in (
        (_CGROUP_V2_MAX, _CGROUP_V2_CURRENT, _CGROUP_V2_STAT, "inactive_file"),
        (_CGROUP_V1_LIMIT, _CGROUP_V1_USAGE, _CGROUP_V1_STAT, "total_inactive_file"),
    ):
        limit = _read_int(limit_path)
        usage = _read_int(usage_path)
        if limit is None or usage is None:
            continue
        # cgroup v2 writes the literal "max" when unlimited; _read_int gives
        # None for that. v1 uses a sentinel near 2**63, which is not a real
        # limit either.
        if limit <= 0 or limit >= (1 << 62):
            continue
        # memory.current / usage_in_bytes COUNT the page cache. Every PDF this
        # worker downloads, hashes and re-reads (a scanned drill log is
        # hundreds of MB) leaves its pages in the cache, and the kernel keeps
        # them until it is under pressure, so on a long-lived worker the
        # "used" figure drifts to the limit while the memory is reclaimable.
        # The guard then read an idle container as full and failed parses
        # with MemoryError. Subtract the inactive file cache, the same
        # working-set definition the kubelet and `docker stats` use.
        usage = max(0, usage - _read_stat_bytes(stat_path, cache_key))
        return max(0.0, (limit - usage) / (1024 * 1024))

    return None


def _cgroup_limit_mb() -> float | None:
    """This container's memory limit in MiB, or None when there is none."""
    for limit_path in (_CGROUP_V2_MAX, _CGROUP_V1_LIMIT):
        limit = _read_int(limit_path)
        if limit is None or limit <= 0 or limit >= (1 << 62):
            continue
        return limit / (1024 * 1024)
    return None


#: Fraction of a container's memory limit that must be free before a parse
#: starts when PARSE_MIN_FREE_RAM_MB is not set, and the clamp on the result.
_PARSE_FREE_RAM_FRACTION = 0.30
_PARSE_FREE_RAM_FLOOR_MB = 1500
_PARSE_FREE_RAM_CAP_MB = 4500


def _default_min_free_mb() -> int:
    """Free-RAM threshold for the pre-parse memory guard when none is configured.

    The fixed 4500 MB default was tuned for a 36 GB dev host. On the 8 GiB
    production worker (the AWS task definition does not set the variable) it
    asks for 56% of the container to be free: one parse already running leaves
    ~3.5 GB, so every SECOND concurrent PDF waited out PARSE_MEMORY_WAIT_MAX_S
    and then failed with MemoryError, twice (retries=1), while the first
    parse was still working. Scaled to the container instead: 30% of its
    limit, clamped to [1500, 4500] MB (8 GiB -> ~2.4 GB, the 2500 the old
    Container Apps deployment pinned by hand). An explicit
    PARSE_MIN_FREE_RAM_MB always wins, and with no cgroup limit the old 4500
    stands.
    """
    limit_mb = _cgroup_limit_mb()
    if limit_mb is None:
        return _PARSE_FREE_RAM_CAP_MB
    return int(max(
        _PARSE_FREE_RAM_FLOOR_MB,
        min(_PARSE_FREE_RAM_CAP_MB, limit_mb * _PARSE_FREE_RAM_FRACTION),
    ))


def _available_memory_mb() -> float | None:
    """Available memory, measured against the container limit where there is one."""
    cgroup_mb = _cgroup_available_mb()
    if cgroup_mb is not None:
        return cgroup_mb

    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        # No cgroup and no psutil: nothing to measure with. Degrade
        # gracefully — the caller still gets the parse done, and OOM risk
        # falls back to the OS killer, which is what we had pre-Phase 5.
        return None

    return psutil.virtual_memory().available / (1024 * 1024)


async def _wait_for_memory_headroom(
    min_free_mb: int,
    max_wait_s: int,
    poll_interval_s: float = 2.0,
) -> None:
    """Block until available memory ≥ ``min_free_mb``, or raise MemoryError
    after ``max_wait_s``.

    "Available" means inside this container's cgroup when there is one — see
    _available_memory_mb. It used to mean psutil.virtual_memory(), which
    measures the host node.

    Used before submitting a parse to the subprocess pool so concurrent
    parses don't pile on and OOM the worker. Polls every
    ``poll_interval_s`` and logs at attempt + retry boundaries.

    When psutil isn't available the function returns immediately
    (degrade gracefully — caller still gets the parse done; OOM risk
    falls back to OS oom-killer, which is what we had pre-Phase 5
    anyway).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait_s
    waited = 0.0
    while True:
        avail_mb = _available_memory_mb()
        if avail_mb is None:
            return  # nothing to measure with; see _available_memory_mb
        if avail_mb >= min_free_mb:
            if waited > 0:
                log.info(
                    "ingest_pdf.memory_guard: cleared after %.1fs "
                    "(available=%.0fMB ≥ threshold=%dMB)",
                    waited, avail_mb, min_free_mb,
                )
            return
        if loop.time() >= deadline:
            raise MemoryError(
                f"ingest_pdf.memory_guard: still {avail_mb:.0f}MB available "
                f"after {waited:.1f}s wait (threshold {min_free_mb}MB) — "
                f"Hatchet will retry on a freer worker"
            )
        log.info(
            "ingest_pdf.memory_guard: waiting for RAM "
            "(available=%.0fMB < threshold=%dMB, waited=%.1fs)",
            avail_mb, min_free_mb, waited,
        )
        await asyncio.sleep(poll_interval_s)
        waited += poll_interval_s


def _get_parse_pool():
    """Lazily create a multi-worker ProcessPoolExecutor for PDF parsing.

    spawn-method to avoid forking a process with a heavy live worker
    state (open sockets, qdrant clients, etc.) — fork would copy them
    and break asyncio internals on Linux. Phase 5 sizes the pool via
    _compute_parse_max_workers() so a batch of N PDFs from one worker
    completes in ~ceil(N / pool_size) × parse_time instead of N ×
    parse_time.
    """
    global _PARSE_POOL
    if _PARSE_POOL is None:
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor
        ctx = mp.get_context("spawn")
        workers = _compute_parse_max_workers()
        _PARSE_POOL = ProcessPoolExecutor(max_workers=workers, mp_context=ctx)
        log.info(
            "ingest_pdf: created ProcessPoolExecutor max_workers=%d (cpu_count=%s)",
            workers, os.cpu_count(),
        )
    return _PARSE_POOL


def _reset_parse_pool() -> None:
    """Tear down the cached ProcessPoolExecutor so the next parse rebuilds.

    Called when a worker dies (BrokenProcessPool) — the pool is poisoned;
    subsequent submit() calls would also fail.

    2026-08-17 — `shutdown(wait=False, cancel_futures=True)` alone leaks
    memory. `cancel_futures=True` only cancels futures that haven't
    started yet; it does NOT touch worker processes that are already
    mid-parse when one of their pool siblings OOM-killed and broke the
    pool. Those survivors keep running — orphaned from the executor we
    just dropped, with nothing left to ever await their result — holding
    onto however much RAM their table extraction had allocated (multi-GB
    for a large NI 43-101) until they happen to finish on their own.
    Confirmed live: a container with PARSE_SUBPROCESS_MAX_WORKERS=2 had 3
    live spawn_main processes (~5.7GB combined RSS) after a prior broken-
    pool reset, permanently starving _wait_for_memory_headroom's 2500MB
    threshold and failing every subsequent parse — the actual cause
    behind a recurring "memory_guard: still N MB available" failure
    pattern, not a genuine capacity/sizing problem.
    ProcessPoolExecutor.kill_workers() (stdlib, Python 3.13+) SIGKILLs
    every still-alive worker before we drop the reference, so a crashed
    pool's memory is reclaimed immediately instead of leaking until
    those orphans happen to exit or the whole container gets OOM-killed.
    """
    global _PARSE_POOL
    if _PARSE_POOL is not None:
        try:
            _PARSE_POOL.kill_workers()
        except AttributeError:
            # Fallback for a stdlib without kill_workers() — best-effort,
            # same leak this fix closes, but never worse than before.
            log.warning(
                "ingest_pdf: ProcessPoolExecutor.kill_workers() unavailable "
                "on this Python version — falling back to shutdown(wait=False), "
                "which does NOT reclaim already-running workers' memory."
            )
            try:
                _PARSE_POOL.shutdown(wait=False, cancel_futures=True)
            except Exception as exc:  # pragma: no cover — best-effort
                log.warning("ingest_pdf: parse pool shutdown raised %s — ignoring", exc)
        except Exception as exc:  # pragma: no cover — best-effort
            log.warning("ingest_pdf: parse pool kill_workers raised %s — ignoring", exc)
        _PARSE_POOL = None


# Temporary directory for PDF bodies consumed by parser subprocesses.
_PDF_BODY_CACHE_DIR = "/tmp/georag_ingest_pdf_cache"

#: Delete anything in the cache dir older than this. Every path that
#: creates a file there also deletes it, but only when its frame actually
#: runs: a preflight that rejects the file, a parse that dies at its
#: hard timeout, a broken pool, a worker SIGKILLed by the cgroup,
#: and a workflow whose preflight and parse landed on DIFFERENT workers
#: all leave a body behind. A few orphaned 1.5 GB atlases fill a worker's
#: ephemeral disk. Eight hours is longer than the parse task's 2 h
#: schedule_timeout plus its longest page-scaled cap (3 h 55 min, HAT-9)
#: plus Hatchet's retry backoff, so this cannot delete a file a live run is
#: still using. It was six hours when the parse cap was a flat 55 minutes.
_PDF_BODY_CACHE_TTL_S = 8 * 3600


def _reap_pdf_body_cache(ttl_s: int = _PDF_BODY_CACHE_TTL_S) -> int:
    """Delete cache files older than ``ttl_s``. Returns the count removed.

    Best-effort and never raises: failing to tidy up must not fail an
    ingest.
    """
    removed = 0
    try:
        cutoff = time.time() - ttl_s
        with os.scandir(_PDF_BODY_CACHE_DIR) as entries:
            for entry in entries:
                try:
                    if entry.is_file() and entry.stat().st_mtime < cutoff:
                        os.unlink(entry.path)
                        removed += 1
                except OSError:
                    continue
    except FileNotFoundError:
        return 0
    except Exception as exc:  # pragma: no cover — best-effort
        log.warning("ingest_pdf: cache reap raised %s — ignoring", exc)
    if removed:
        log.info("ingest_pdf: reaped %d stale body cache file(s)", removed)
    return removed


def _hash_file(path: str) -> tuple[str, int]:
    """sha256 + size of a file, read in chunks. Blocking; call in a thread."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


#: Upload-stack ceiling: GEORAG_MAX_UPLOAD_BYTES (512 MiB by default), the
#: same variable Laravel's Uploads::maxBytes() reads. A hard-coded 2 GiB used to
#: stand here, "mirroring" a ceiling that had since been lowered, so nothing
#: the web tier admitted could ever trip it.
_MAX_PDF_BYTES = max_upload_bytes()


def _read_head(path: str, n: int) -> bytes:
    """First ``n`` bytes of a file. Blocking; call in a thread."""
    with open(path, "rb") as fh:
        return fh.read(n)


async def _declared_object_size(minio_key: str) -> int | None:
    """Size from a HEAD, or None when the backend will not say.

    None means "proceed and check the real size after downloading": a
    HEAD that fails is not itself a reason to reject an upload, and the
    GET that follows will fail for the same reason if the object is
    genuinely unreachable.
    """
    try:
        storage = get_async_storage_client()
        meta = await storage.head(Bucket.BRONZE, minio_key)
        size = meta.get("size") if isinstance(meta, dict) else None
        return int(size) if size is not None else None
    except Exception as exc:
        log.info(
            "ingest_pdf.preflight: HEAD failed for %s (%s) — falling back "
            "to the post-download size check", minio_key, exc,
        )
        return None


async def _download_to_cache(minio_key: str) -> tuple[str, str, int]:
    """Stream an object to a run-scoped cache file.

    Returns ``(path, sha256, size)``. The bytes never exist as one object
    in this process: the storage client chunks them into the file handle
    and the hash is computed by re-reading the file in 1 MiB blocks.

    The filename is a run-unique uuid, not the sha — two concurrent
    parses of the SAME file must not delete each other's input, and the
    sha is not known until after the download anyway.
    """
    os.makedirs(_PDF_BODY_CACHE_DIR, exist_ok=True)
    _reap_pdf_body_cache()
    path = f"{_PDF_BODY_CACHE_DIR}/body.{uuid.uuid4().hex}.pdf"
    tmp_path = path + ".tmp"
    storage = get_async_storage_client()
    try:
        await storage.get_file(Bucket.BRONZE, minio_key, tmp_path)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(Exception):
            os.unlink(tmp_path)
        raise
    sha256, size = await asyncio.to_thread(_hash_file, path)
    return path, sha256, size


async def _resolve_body_path(minio_key: str, pre: dict) -> str:
    """Path of the body for a parse, downloading it only if we must.

    Preflight streams the body to the cache and passes its path forward,
    so the common case costs no second download at all. But Hatchet is
    free to run preflight and parse on DIFFERENT workers, where that path
    does not exist — a missing file is a normal condition here, not an
    error, and we fetch it again.
    """
    cached = (pre or {}).get("body_path")
    if cached and os.path.exists(cached):
        return cached
    if cached:
        log.info(
            "ingest_pdf.parse: preflight body %s is not on this worker — "
            "re-downloading key=%s", cached, minio_key,
        )
    path, _sha, _size = await _download_to_cache(minio_key)
    return path

# Phase 2 (2026-06-24): when set, persist captions each figure with the
# Qwen3-VL sidecar (an S3 GET + a VL call per figure) and folds the description
# into the figure's ReportSection text before embedding. Off by default — shares
# the FIGURE_VL_DESCRIPTIONS switch with the standalone figure_extractor path.
_FIGURE_VL_CAPTIONS = (os.environ.get("FIGURE_VL_DESCRIPTIONS") or "").strip().lower() in (
    "1", "true", "yes", "on",
)


def _run_parser_subprocess(
    body_path: str, sha256: str, progress_file: str | None = None,
) -> dict:
    """Module-level wrapper for the parse so ProcessPoolExecutor can pickle it.

    Takes the PATH of an already-downloaded body, not the bytes. Passing
    bytes meant the whole file was pickled through the pool's pipe (a
    second copy in the parent's send buffer), materialised a third time
    in the child, and then written straight back out to /tmp anyway — so
    a 1.5 GB map atlas that passes the 2 GB cap needed several GB of
    headroom on an 8 Gi worker to parse a file the parser only ever
    reads from disk. Now only the path crosses the boundary.

    The caller owns the file and deletes it; see `_parse_body`'s finally.
    Deleting it here would be wrong now that preflight may have created
    it for a parse that has not started yet.

    Returns a plain dict (not a Pydantic model) — easier to pickle across
    process boundaries; caller reconstitutes ParseOut.

    The parser's `figure_manifest` field would list any figure already
    uploaded to figures/_pending/{sha256}/... by a manifest producer,
    propagated to the persist task via the returned dict; persist renames
    each pending key to figures/{report_id}/... before recording the
    section. Currently always empty — see the note in
    app.services.ingest.pdf_report.ReportParseResult.figure_manifest.
    """
    import time as _time

    from app.services.ingest.pdf_report import parse_pdf_report

    cached_path = body_path

    try:
        t_start = _time.monotonic()
        result = parse_pdf_report(cached_path, progress_file=progress_file)
        elapsed_ms = int((_time.monotonic() - t_start) * 1000)

        _sections_out = [
            {
                "section_number": getattr(s, "section_number", None),
                "section_title": getattr(s, "section_title", None),
                "text": getattr(s, "text", None),
                "page_first": getattr(s, "page_first", None),
                "page_last": getattr(s, "page_last", None),
                # Phase 3 (2026-05-22) — OCR provenance per chunk.
                # None for chunks that came from the PDF text layer
                # (fitz_native, pdfplumber_native); 0.0–1.0 for
                # OCR'd chunks. Travels through ParseOut → persist
                # → silver.document_passages → qdrant payload.
                "ocr_confidence": getattr(s, "ocr_confidence", None),
                "ocr_method": getattr(s, "ocr_method", None),
            }
            for s in (getattr(result, "sections", None) or [])
        ]

        # Multimodal page renders (2026-08-18). Done here, in the parse task,
        # because this is the only step with the PDF on local disk. Uploads
        # land under a _pending key; persist renames them once report_id
        # exists — same two-phase shape as the figure manifest above.
        # Fail-soft: stage_page_images swallows per-page errors and returns
        # whatever it got, so a render problem can't fail a good text parse.
        from app.services.ingest.page_image import stage_page_images

        # Pages the engine read that ALSO had a text layer (PDF_PARSE_MODE=all):
        # the `figures` scope needs them to tell a text page from a figure.
        _engine_text_pages: set[int] = set()
        for _w in getattr(result, "warnings", None) or []:
            if isinstance(_w, dict) and _w.get("code") == "pdf_parse_mode_summary":
                _engine_text_pages.update(int(x) for x in _w.get("engine_text_pages") or [])
        _page_image_warnings: list[dict] = []
        try:
            _page_images = stage_page_images(
                cached_path, sha256, _sections_out,
                engine_text_pages=_engine_text_pages,
                warnings_out=_page_image_warnings,
            )
        except Exception as _pi_exc:  # noqa: BLE001
            log.warning(
                "ingest_pdf: page-image staging failed for %s: %s", sha256, _pi_exc,
            )
            _page_images = []
            _page_image_warnings.append({
                "code": "page_image_staging_failed",
                "severity": "warning",
                "detail": f"Page-image staging failed for the whole document: {_pi_exc}",
            })

        return {
            "sha256": sha256,
            "page_image_manifest": _page_images,
            "page_image_warnings": _page_image_warnings,
            "title": getattr(result, "title", None),
            "authors": list(getattr(result, "authors", []) or []),
            "company": getattr(result, "company", None),
            "filing_date": getattr(result, "filing_date", None),
            "commodity": getattr(result, "commodity", None),
            "project_name": getattr(result, "project_name", None),
            "region": getattr(result, "region", None),
            "sections": _sections_out,
            "parse_quality_pct": float(getattr(result, "parse_quality_pct", 0.0) or 0.0),
            # The number people believe parse_quality_pct is. See
            # ReportParseResult.text_page_coverage_pct.
            "text_page_coverage_pct": float(
                getattr(result, "text_page_coverage_pct", 0.0) or 0.0
            ),
            # None is "not computed" (the unparseable-document early return)
            # and stays None: persist writes NULL, not 0.0. It used to be
            # dropped right here, so silver.reports.extraction_confidence read
            # NULL on every row although persist's INSERT reads it.
            "extraction_confidence": getattr(result, "extraction_confidence", None),
            "parser_used": str(getattr(result, "parser_used", "unknown") or "unknown"),
            "skipped_elements": int(getattr(result, "skipped_elements", 0) or 0),
            "warnings": [
                w if isinstance(w, dict) else {"message": str(w)}
                for w in (getattr(result, "warnings", None) or [])
            ],
            "page_languages": list(getattr(result, "page_languages", []) or []),
            "resource_tables": list(getattr(result, "resource_tables", []) or []),
            "figures": list(getattr(result, "figure_manifest", []) or []),
            "parse_duration_ms": elapsed_ms,
            "is_scanned": bool(getattr(result, "is_scanned", False)),
        }
    finally:
        # Intentionally does NOT unlink cached_path — the parse task owns
        # the file (preflight may have created it) and removes it in its
        # own finally, which also covers the hard-timeout and
        # broken-pool paths where this frame never runs at all.
        pass


# =============================================================================
# Input + per-step output models
# =============================================================================
class IngestPdfInput(BaseModel):
    """The Laravel ShadowRouter (Step 5) sends this when dual-writing.

    `project_id` is typed `str` (not `UUID`) so the many downstream
    `str(input.project_id)` call sites stay no-ops, but a Pydantic
    field_validator rejects non-UUID input at the boundary —
    defence in depth against the SQL-injection shape the
    2026-06-02/03 audit caught on the sibling ingest_zip_archive
    trigger. The shadow_trigger router uses parameter binding so a
    malformed string can't actually inject, but this guard prevents
    malformed rows from ever landing in silver.ingest_progress.
    """

    workspace_id: UUID = Field(..., description="Workspace context for RLS.")
    project_id: str = Field(..., description="Project the upload belongs to.")
    minio_key: str = Field(..., description="Bronze S3 key (reports/{projectId}/...).")
    file_size: int = Field(..., description="Bytes (from Laravel multipart upload).")
    vendor_profile_id: int | None = Field(default=None)
    correlation_token: str = Field(
        ..., description="Shared token for shadow_runs row pairing — also the dedupe key."
    )
    actor_id: int | None = Field(default=None, description="public.users.id of uploader.")
    # The silver.ingest_progress row id the caller claimed before dispatch
    # (Laravel mints it per upload, the trigger endpoint writes the queued
    # row under it, ingest_zip_archive and the stale sweep do the same for
    # their children). parse and persist heartbeat THIS row by id. Before
    # 2026-10-04 the field did not exist: every stage re-resolved the row
    # from (workspace, key) on every tick, and a newer non-terminal row for
    # the same key (a sweep child, a re-upload) took the heartbeats while
    # the row of the parse actually running went stale. Optional because
    # tiff_normalize dispatches the derived PDF without one and preflight
    # then mints the row.
    run_id: str | None = Field(
        default=None,
        description="Caller-claimed silver.ingest_progress run_id (uuid4 string).",
    )

    # Defence-in-depth UUID guard; see class docstring.
    from pydantic import field_validator as _fv

    @_fv("project_id", "run_id")
    @classmethod
    def _validate_uuid_fields(cls, v: str | None, info) -> str | None:
        import re as _re
        if v is None:
            return v
        if not _re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            v,
            _re.IGNORECASE,
        ):
            raise ValueError(
                f"IngestPdfInput.{info.field_name} must be a UUID "
                "(canonical 8-4-4-4-12 form)."
            )
        return v


class PreflightOut(BaseModel):
    sha256: str
    page_count: int
    file_size: int
    encrypted: bool
    valid: bool
    error: str | None = None
    #: Where preflight left the downloaded body on the worker that ran
    #: it. The parse task reads it from here instead of downloading the
    #: file a second time — but Hatchet may schedule the two tasks on
    #: different workers, so parse treats a missing path as a normal
    #: cache miss and re-fetches. See `_resolve_body_path`.
    body_path: str | None = None


class ParseOut(BaseModel):
    """Mirror of the relevant v1.49 ReportParseResult fields, serialised."""

    sha256: str
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    company: str | None = None
    filing_date: str | None = None
    commodity: str | None = None
    project_name: str | None = None
    region: str | None = None
    sections: list[dict] = Field(default_factory=list)
    parse_quality_pct: float = 0.0
    # NI 43-101 heading coverage and page-level extraction coverage are
    # different questions; carrying only the first is what let a document
    # whose 300 pages OCR'd to nothing report as well parsed.
    text_page_coverage_pct: float = 0.0
    # Composite 0-1 extraction confidence from the parser; None = not computed.
    # Without this field pydantic dropped the key between parse and persist.
    extraction_confidence: float | None = None
    parser_used: str = ""
    skipped_elements: int = 0
    warnings: list[dict] = Field(default_factory=list)
    page_languages: list[str] = Field(default_factory=list)
    resource_tables: list[dict] = Field(default_factory=list)
    # Figure manifest. Each entry would be a dict {idx, page, bbox, caption,
    # pending_key, bucket, sha256}, populated by _run_parser_subprocess from
    # ReportParseResult.figure_manifest. Persist task copies each
    # pending_key to figures/{report_id}/... then deletes the pending
    # object, and builds a ReportSection per figure for chat retrieval.
    # Currently always empty — see the note in
    # app.services.ingest.pdf_report.ReportParseResult.figure_manifest.
    figures: list[dict] = Field(default_factory=list)
    # Page-image renders staged under page-images/_pending/... by
    # _run_parser_subprocess, and the warnings staging produced. These two were
    # returned by the subprocess but had no field here, and pydantic drops
    # unknown keys, so persist's `parsed.get("page_image_manifest")` was always
    # empty: every render was uploaded and none was ever finalised.
    page_image_manifest: list[dict] = Field(default_factory=list)
    page_image_warnings: list[dict] = Field(default_factory=list)
    parse_duration_ms: int = 0
    is_scanned: bool = False
    #: Set when the heavy fields above were too big to ship as task output and
    #: were packed here (gzip + base64 of their JSON), leaving them empty. See
    #: :func:`_pack_parse_output`; persist reads through
    #: :func:`_unpack_parse_output`.
    heavy_gz_b64: str | None = None


#: The fields that carry a document's bulk. Everything else on ParseOut is a
#: few bytes and always stays inline (``parser_used`` is read by embed_verify).
_PARSE_HEAVY_FIELDS = frozenset({
    "sections", "resource_tables", "figures", "page_image_manifest",
    "warnings", "page_image_warnings", "page_languages",
})

#: A task's output travels to the engine, and on to every child task, as one
#: gRPC message, and hatchet_sdk caps those at 4 MiB by default
#: (config.grpc_max_send_message_length / _recv_). parse used to return the whole
#: document as that output. A 500-page report with tables can exceed 4 MiB; the
#: send then failed at the END of a 4-hour task that had already paid for every
#: OCR page, and retries=1 re-ran the parse (and the OCR bill) for the same
#: failure. Up to INLINE the output is returned exactly as before; above it the
#: heavy fields are packed; if even packed the output would not fit, the parse
#: fails once, with the reason, instead of failing opaquely and again.
def _env_bytes(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name) or default))
    except ValueError:
        log.warning("ingest_pdf: %s=%r is not an integer; using %d", name, os.environ.get(name), default)
        return default


PARSE_OUTPUT_INLINE_MAX_BYTES = _env_bytes("PARSE_OUTPUT_INLINE_MAX_BYTES", 2 * 1024 * 1024)
PARSE_OUTPUT_MAX_BYTES = _env_bytes("PARSE_OUTPUT_MAX_BYTES", 3 * 1024 * 1024)


def _bronze_bucket_name() -> str:
    """The bronze bucket the object storage layer actually reads and writes.

    ``AWS_BUCKET_BRONZE`` (``georag-bronze-<account>`` on AWS), through the same
    resolution as every storage call. The OCR review rows recorded
    ``s3://$MINIO_BUCKET_BRONZE/...``, and Terraform sets AWS_BUCKET_BRONZE, not
    the MINIO_ name, so on AWS they pointed at a bucket called ``bronze`` that does
    not exist.
    """
    try:
        return StorageConfig.from_env().bucket_name(Bucket.BRONZE)
    except ValueError:
        # Half a credential pair. Every storage call will say so; a URI that is
        # only a record of where the file is must not be what fails persist.
        return os.environ.get("AWS_BUCKET_BRONZE") or Bucket.BRONZE.value


def _serialised_size(out: BaseModel) -> int:
    return len(out.model_dump_json().encode("utf-8"))


def _pack_parse_output(out: ParseOut) -> ParseOut:
    """Make a ParseOut small enough to be a task output, losslessly.

    Unchanged below ``PARSE_OUTPUT_INLINE_MAX_BYTES``. Above it the heavy fields
    are gzipped into ``heavy_gz_b64`` (extracted text compresses five- to
    tenfold) and emptied. If the packed result still exceeds
    ``PARSE_OUTPUT_MAX_BYTES``, raises NonRetryableException: a retry would
    re-OCR every page and fail the same way.
    """
    raw = _serialised_size(out)
    if raw <= PARSE_OUTPUT_INLINE_MAX_BYTES:
        return out
    heavy = out.model_dump(mode="json", include=set(_PARSE_HEAVY_FIELDS))
    blob = base64.b64encode(
        gzip.compress(json.dumps(heavy, separators=(",", ":")).encode("utf-8"), compresslevel=6)
    ).decode("ascii")
    packed = out.model_copy(
        update={**{field: [] for field in _PARSE_HEAVY_FIELDS}, "heavy_gz_b64": blob},
    )
    size = _serialised_size(packed)
    if size > PARSE_OUTPUT_MAX_BYTES:
        raise NonRetryableException(
            f"parse output is {raw} bytes ({size} compressed), over the {PARSE_OUTPUT_MAX_BYTES}-byte "
            "limit a task output can carry; not retried, a retry would re-OCR every page and fail "
            "the same way. Raise PARSE_OUTPUT_MAX_BYTES together with the Hatchet client's "
            "HATCHET_CLIENT_GRPC_MAX_SEND/RECV_MESSAGE_LENGTH if documents this large are expected."
        )
    log.warning(
        "ingest_pdf.parse: output %d bytes exceeded the %d-byte inline limit; packed to %d bytes",
        raw, PARSE_OUTPUT_INLINE_MAX_BYTES, size,
    )
    return packed


def _unpack_parse_output(parsed: dict[str, Any]) -> dict[str, Any]:
    """The inverse of :func:`_pack_parse_output`, on a ``model_dump()``. A no-op
    for an output that was never packed."""
    blob = parsed.get("heavy_gz_b64")
    if not blob:
        return parsed
    heavy = json.loads(gzip.decompress(base64.b64decode(blob)))
    return {**parsed, **heavy, "heavy_gz_b64": None}


class IngestPdfFinalOut(BaseModel):
    """The final output emitted to Hatchet's run record. Mirrors the v1.49
    ReportParseResult shape so the diff harness can compare apples-to-apples.
    """

    sha256: str
    parser_used: str
    parse_quality_pct: float
    page_count: int
    title: str | None
    authors: list[str]
    company: str | None
    filing_date: str | None
    commodity: str | None
    project_name: str | None
    region: str | None
    sections_count: int
    resource_tables_count: int
    is_scanned: bool
    warnings_count: int
    parse_duration_ms: int
    persist_duration_ms: int
    report_id: str | None = None
    shadow_runs_id: str | None = None
    # Phase 1 R-P1-4 — silver.document_passages writer. One row per
    # parsed section (chunk_kind='narrative'), ordinal-ordered. Layout-
    # aware chunking (page_first/last + bbox + chunk_kind='table' /
    # 'caption_figure') is Phase 2 ingestion-pipeline work.
    passages_written: int = 0
    #: What the run owes the user, built by :func:`build_run_warnings` and
    #: stored on silver.ingest_progress. embed_verify closes the run WITH
    #: these; the embed completion sweep and stale_run_detector read the same
    #: list back from the row.
    run_warnings: list[dict] = Field(default_factory=list)

# One DSN builder for the whole service — see app/db/dsn.py for why
# sixty copies of this existed and what the drift cost.
_dsn = build_dsn


# =============================================================================
# Workflow + steps
# =============================================================================
ingest_pdf = hatchet.workflow(
    name="ingest_pdf",
    input_validator=IngestPdfInput,
    # 2026-05-23 — per-workspace singleton. At the time, the parse step
    # loaded docling/PaddleOCR/RapidOCR models (~3-4 GB resident); running
    # multiple concurrent parses on the 36 GB host pushed total memory
    # over the edge and the OOM killer fired SIGKILL on the youngest
    # parse subprocess. Confirmed root cause of the
    # "A child process terminated abruptly" failures observed during
    # the 2026-05-23 TIFF smoke (see [[tiff-smoke-2026-05-23]]). docling
    # was removed 2026-07-29 (never ran in production), but the
    # concurrency limit stays — same-workspace serialisation is still the
    # right behavior for large-batch re-ingests regardless of parser cost.
    #
    # GROUP_ROUND_ROBIN queues rather than cancels — a long real PDF
    # parse can't be interrupted by a smaller upload behind it.
    # Different workspaces still parse in parallel; only same-workspace
    # uploads serialise.
    #
    # IMPORTANT: every task in this workflow MUST set schedule_timeout
    # ≥ "2h" (see decorators below). Hatchet's default schedule_timeout
    # is 5 minutes — under this per-workspace serialisation any workflow
    # that waits in the queue longer than 5 min gets silently CANCELLED
    # (no error message, retry_count=0, worker_id=null). With 5+ PDFs
    # triggered in one burst that's roughly half the batch lost. The
    # 2026-05-24 Ontario Gold re-ingest hit this — runs 5–9 of 9 all
    # cancelled at exactly the 5-min mark. schedule_timeout="2h" gives
    # space for ~80 sequential parses before the tail starts expiring.
    # 2026-08-07 — raised 1 → 2. The original OOM driver (docling/Paddle
    # models resident per parse) is gone: OCR is remote (Cohere Parse) and
    # embedding is hosted (Embed 5 Pro). Two in-flight runs
    # let doc B parse while doc A persists/embeds, roughly halving batch
    # wall-clock; PARSE_SUBPROCESS_MAX_WORKERS and the memory guard still
    # bound actual parse concurrency on small containers.
    concurrency=ConcurrencyExpression(
        expression="input.workspace_id",
        max_runs=2,
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)


# ---- Step 1: preflight -------------------------------------------------------
# 2026-08-10: execution_timeout raised 60s -> 180s. Preflight is mostly a
# ~20 MB blob download + sha256, but on a saturated worker (concurrent parse
# using all cores) the event loop starves and the old 60s budget expired 3×
# in a row, terminally failing a whole workflow (Madsen, 2026-08-07 11:32Z).
#
# backoff_* on this task and the three below (2026-10-10): every one of them
# calls something remote (S3, Postgres, Qdrant/Cohere through the embed
# dispatch), and Hatchet retries IMMEDIATELY unless it is given a backoff, so a
# transient outage spent the whole retry budget in a few milliseconds. The delay
# before a retry is factor ** (retries so far), capped: about 1-8 s before the
# first retry and up to a minute before the second.
@ingest_pdf.task(
    execution_timeout="180s", schedule_timeout="2h", retries=2,
    backoff_factor=8.0, backoff_max_seconds=60,
)
async def preflight(input: IngestPdfInput, ctx: Context) -> PreflightOut:
    """Stream from S3 to disk, hash it, validate magic bytes + size + encryption.

    The body is streamed to a run-scoped file under _PDF_BODY_CACHE_DIR and
    its path is returned on PreflightOut, so the parse task does not have
    to download the same object a second time. Nothing here ever holds the
    whole file in memory — the size cap, the magic bytes, the /Encrypt
    probe and the page count are all answered from the file on disk.
    """
    log.info("ingest_pdf.preflight start key=%s", input.minio_key)

    # CC-03 Item 8 — lifecycle guard. If the project is not active, skip
    # the whole workflow by returning a synthetic PreflightOut that marks
    # the run as invalid. We don't raise — Hatchet would retry a raise,
    # burning retries for something that won't change until the project
    # is reactivated. Returning early with valid=False causes parse() to
    # short-circuit and persist() to produce an empty report with a
    # descriptive warning; the operator can see the skip_reason in the
    # Hatchet run record.
    if input.project_id:
        _skip_reason: str | None = None
        try:
            _lc_pool = await asyncpg.create_pool(
                _dsn(), min_size=1, max_size=1, statement_cache_size=0
            )
            try:
                async with _lc_pool.acquire() as _lc_conn:
                    async with _lc_conn.transaction():
                        if input.workspace_id:
                            await bind_workspace_scope(
                                _lc_conn, workspace_id=str(input.workspace_id), site="hatchet.ingest_pdf"
                            )
                        _lc_row = await _lc_conn.fetchrow(
                            "SELECT lifecycle_state FROM silver.projects "
                            "WHERE project_id = $1::uuid",
                            str(input.project_id),
                        )
                        if _lc_row is not None:
                            _state = _lc_row["lifecycle_state"]
                            if _state != "active":
                                _skip_reason = f"project_not_active:{_state}"
            finally:
                await _lc_pool.close()
        except Exception as _lc_err:
            # Connection failure or table-not-found (e.g. first-run before
            # migration). Log and proceed — fail open is safer than silently
            # dropping every ingest on startup errors.
            log.warning(
                "ingest_pdf.preflight: lifecycle check failed (non-fatal): %s", _lc_err
            )

        if _skip_reason:
            log.info(
                "ingest_pdf.preflight: skipping workflow — project=%s reason=%s",
                input.project_id,
                _skip_reason,
            )
            return PreflightOut(
                sha256="",
                page_count=0,
                file_size=0,
                encrypted=False,
                valid=False,
                error=_skip_reason,
            )

    if input.project_id and input.workspace_id:
        await ingest_progress.mark_started(
            workspace_id=str(input.workspace_id),
            project_id=str(input.project_id),
            minio_key=input.minio_key,
            step="preflight",
            workflow_run_id=getattr(ctx, "workflow_run_id", None),
            run_id=input.run_id,
        )
    # Hard cap: GEORAG_MAX_UPLOAD_BYTES, the same ceiling the upload stack
    # (OCTANE_MAX_REQUEST_SIZE / PHP_UPLOAD_MAX_FILESIZE / Laravel validator)
    # applies. It was a hard-coded 2 GB (raised 2026-05-22 from 100 MB).
    #
    # Checked against the object's declared size BEFORE downloading. The
    # cap used to be applied to `len(body)` after the whole file was
    # already resident, which is the one place it could not help: a 4 GB
    # upload had to be pulled into RAM in full before we were willing to
    # say it was too big. HEAD is one request and costs nothing.
    declared_size = await _declared_object_size(input.minio_key)
    if declared_size is not None and declared_size > _MAX_PDF_BYTES:
        return PreflightOut(
            sha256="", page_count=0, file_size=declared_size,
            encrypted=False, valid=False,
            error=f"PDF exceeds {human_bytes(_MAX_PDF_BYTES)} (got {declared_size})",
        )

    body_path, sha256, file_size = await _download_to_cache(input.minio_key)
    keep_body = False
    try:
        # Re-checked against what actually arrived: HEAD is metadata and
        # a re-uploaded object can disagree with it.
        if file_size > _MAX_PDF_BYTES:
            return PreflightOut(
                sha256=sha256, page_count=0, file_size=file_size,
                encrypted=False, valid=False,
                error=f"PDF exceeds {human_bytes(_MAX_PDF_BYTES)} (got {file_size})",
            )

        header = await asyncio.to_thread(_read_head, body_path, 8192)
        if not header.startswith(b"%PDF-"):
            return PreflightOut(
                sha256=sha256, page_count=0, file_size=file_size,
                encrypted=False, valid=False,
                error="missing %PDF- magic bytes",
            )

        # Encryption detection: bare `/Encrypt` substring match (the old
        # logic) rejected NI 43-101 PDFs that merely had a "no copy"
        # permission flag but extracted fine (Madsen PFS was a casualty).
        # Real test: can we actually open + count pages?  If pikepdf can
        # read it, downstream fitz/pdfplumber can extract from it.
        encrypted_flag = b"/Encrypt" in header

        def _count_pages() -> tuple[int, bool, str | None]:
            import pikepdf
            try:
                # Opened by PATH: pikepdf maps the file rather than
                # taking a BytesIO over a full in-memory copy.
                with pikepdf.open(body_path) as pdf:
                    return len(pdf.pages), False, None
            except pikepdf.PasswordError as e:
                return 0, True, f"password-protected: {e}"
            except Exception as e:
                return 0, encrypted_flag, f"pikepdf open failed: {e}"

        page_count, password_protected, open_error = await asyncio.to_thread(
            _count_pages,
        )

        # Only reject when the PDF is genuinely password-protected (can't
        # open without a passphrase). Permission-flagged PDFs that pikepdf
        # opens successfully proceed to extraction.
        if password_protected:
            return PreflightOut(
                sha256=sha256, page_count=0, file_size=file_size,
                encrypted=True, valid=False,
                error=open_error or "PDF is password-protected",
            )

        # The only path that hands the body on to parse — every return
        # above is a rejection, and the finally below deletes the file for
        # all of them.
        keep_body = True
        return PreflightOut(
            sha256=sha256,
            page_count=page_count,
            file_size=file_size,
            encrypted=encrypted_flag,
            valid=True,
            error=None,
            body_path=body_path,
        )
    finally:
        if not keep_body:
            with contextlib.suppress(Exception):
                os.unlink(body_path)


# ---- Step 2: parse — single call to v1.49 parse_pdf_report ------------------
#: HAT-9 (2026-09-29) — the parse wall cap scales with page count.
#:
#: It was a flat 3300 s (55 min) whatever the document. Cohere Parse OCRs one
#: page per request, PDF_OCR_PAGE_CONCURRENCY at a time, so a large scanned
#: NI 43-101 (1,000+ image pages) could not finish inside it. It timed out,
#: the retry re-OCRed from page 1 (nothing is cached between attempts), timed
#: out again, and the document failed after ~2 h having paid for Parse twice.
#:
#: The cap is now ``max(base, overhead + pages * seconds_per_page)``, bounded
#: by PARSE_WALL_CAP_MAX_S, which sits just under the task's execution_timeout
#: so our own timeout (which resets the pool) still fires before Hatchet's.
#: Nothing has measured Parse throughput yet (its wire shape is unverified;
#: CLAUDE.md), so the per-page figure is an env-tunable guess. Set it from
#: the first real Parse run (ops/validation/cohere_probe.py).
PARSE_WALL_CAP_BASE_S = 3300
PARSE_WALL_CAP_MAX_S = 14_100  # 3 h 55 min, under execution_timeout="4h"
_PARSE_OVERHEAD_S = 600
_DEFAULT_PARSE_SECONDS_PER_PAGE = 6.0


def _parse_wall_cap_s(page_count: int | None) -> int:
    """Seconds the parse subprocess may run for a document of ``page_count``.

    Never below the old flat 3300 s, never above PARSE_WALL_CAP_MAX_S.
    ``PDF_PARSE_SECONDS_PER_PAGE`` tunes the slope.
    """
    try:
        per_page = float(
            os.environ.get("PDF_PARSE_SECONDS_PER_PAGE", _DEFAULT_PARSE_SECONDS_PER_PAGE),
        )
    except ValueError:
        log.warning(
            "ingest_pdf: PDF_PARSE_SECONDS_PER_PAGE=%r is not a number; using %s",
            os.environ.get("PDF_PARSE_SECONDS_PER_PAGE"),
            _DEFAULT_PARSE_SECONDS_PER_PAGE,
        )
        per_page = _DEFAULT_PARSE_SECONDS_PER_PAGE
    if per_page <= 0:
        per_page = _DEFAULT_PARSE_SECONDS_PER_PAGE
    pages = max(int(page_count or 0), 0)
    scaled = int(_PARSE_OVERHEAD_S + pages * per_page)
    return max(PARSE_WALL_CAP_BASE_S, min(scaled, PARSE_WALL_CAP_MAX_S))


# execution_timeout 60m -> 4h (HAT-9): the outer bound for the scaled cap
# above. The in-process cap is what normally fires; this only has to be
# larger. A static bound rather than ctx.refresh_timeout, because nothing
# here has confirmed the pinned hatchet-lite engine honours a refresh, and a
# silently ignored refresh would let Hatchet kill the task mid-parse with a
# poisoned pool, the exact failure the in-process cap exists to prevent.
@ingest_pdf.task(
    execution_timeout="4h", schedule_timeout="2h", retries=1, parents=[preflight],
    backoff_factor=8.0, backoff_max_seconds=60,
)
async def parse(input: IngestPdfInput, ctx: Context) -> ParseOut:
    """Call the canonical v1.49 ``parse_pdf_report`` end to end.

    The parser owns: fitz-first → Cohere Parse/Tesseract OCR routing → pdfplumber
    fallback → OCR (if scanned) → metadata extraction → section split → resource table extract.
    Returns a ReportParseResult; we serialise it into ParseOut.
    """
    if input.project_id and input.workspace_id:
        await ingest_progress.mark_started(
            workspace_id=str(input.workspace_id),
            project_id=str(input.project_id),
            minio_key=input.minio_key,
            step="parse",
            run_id=input.run_id,
        )
    pre = ctx.task_output(preflight)
    pre = pre.model_dump() if hasattr(pre, "model_dump") else dict(pre)

    if not pre.get("valid"):
        return ParseOut(
            sha256=pre.get("sha256", ""),
            parser_used="skipped",
            warnings=[{"code": "preflight_rejected", "message": pre.get("error", "")}],
        )

    # Reliability spec Fix 1d — heartbeat every 30s so the stale-run
    # detector knows we're alive on multi-minute parses. The async ctxmgr
    # cancels the ticker on exit (normal + exception path).
    async with ingest_progress.heartbeat_loop(
        run_id=input.run_id,
        workspace_id=str(input.workspace_id),
        minio_key=input.minio_key,
    ):
        return await _parse_body(input, pre)


async def _parse_body(input: IngestPdfInput, pre: dict) -> ParseOut:
    """Inner body of parse() — wrapped so heartbeat_loop can manage the
    ticker around the entire blocking-subprocess section."""
    body_path = await _resolve_body_path(input.minio_key, pre)

    log.info("ingest_pdf.parse start key=%s", input.minio_key)
    # Run in subprocess (separate GIL) so heartbeats stay alive even on
    # heavy 500-page parses with per-page OCR. Fallback to thread on
    # subprocess failure (e.g. unpicklable internal state) since the
    # work still has to happen.
    #
    # Phase 5 (2026-05-22) — pre-fork memory guard. Blocks until the
    # worker has enough free RAM to safely run a parse alongside any
    # already-running ones. Raises MemoryError on timeout; Hatchet's
    # retries=1 will retry on a freer worker.
    # The threshold defaults to 30% of the container's memory limit,
    # clamped to [1500, 4500] MB (see _default_min_free_mb); 4500 is the
    # figure tuned for the 36 GB dev host. The 120 s wait budget gives a
    # transient pressure spike room to clear before the workflow gives up
    # and lets Hatchet retry. See [[tiff-smoke-2026-05-23]] for the
    # root-cause analysis.
    try:
        _min_free_mb = int(os.environ.get("PARSE_MIN_FREE_RAM_MB") or _default_min_free_mb())
    except ValueError:
        _min_free_mb = _default_min_free_mb()
    try:
        _max_wait_s = int(os.environ.get("PARSE_MEMORY_WAIT_MAX_S", "120"))
    except ValueError:
        _max_wait_s = 120
    await _wait_for_memory_headroom(
        min_free_mb=_min_free_mb, max_wait_s=_max_wait_s,
    )

    loop = asyncio.get_running_loop()
    pool = _get_parse_pool()

    # Page-level progress relay: the subprocess writes {phase, done, total}
    # to a beacon file (see pdf_report._tick_progress); this task polls it
    # and folds it into silver.ingest_progress.stage_pct so the UI bar
    # moves during the parse instead of sitting at the step boundary.
    # Weighting inside the parse stage (2026-08-14 — added the 'tables'
    # band): text extraction is fast (0–15%), per-page OCR is the long
    # pole on scanned docs (15–75%), and the table-extraction pass is the
    # long pole on NATIVE docs (75–100%) — previously the bar sat frozen
    # at the end of extract for the whole table pass. Bands follow the
    # chronological phase order in parse_pdf_report (extract → ocr →
    # tables) so the relayed pct stays monotonic.
    _progress_path = f"{_PDF_BODY_CACHE_DIR}/progress.{uuid.uuid4().hex}.json"

    async def _relay_progress() -> None:
        # The caller-claimed row when there is one. Resolving by (workspace,
        # key) alone can land on a newer non-terminal row for the same file (a
        # sweep child, a re-upload), which is the mix-up IngestPdfInput.run_id
        # was added to end: progress bars and heartbeats written to a sibling
        # row while this run's own row shows nothing.
        run_id: str | None = input.run_id
        import json as _json
        while True:
            await asyncio.sleep(3)
            # Resolve (and re-resolve) the run row inside the loop: a lookup
            # that failed once used to end this relay for the whole parse, so
            # a multi-hour scanned PDF showed no progress at all.
            if run_id is None:
                if not input.workspace_id:
                    return
                try:
                    run_id = await ingest_progress.lookup_active_run_id(
                        workspace_id=str(input.workspace_id),
                        minio_key=input.minio_key,
                    )
                except Exception:  # noqa: BLE001 - relay is best-effort
                    log.debug("ingest_pdf.parse: run lookup failed", exc_info=True)
                    run_id = None
                if run_id is None:
                    continue
            try:
                with open(_progress_path, encoding="utf-8") as fh:
                    beat = _json.load(fh)
            except Exception:
                continue
            phase = beat.get("phase")
            done, total = int(beat.get("done", 0)), max(1, int(beat.get("total", 1)))
            if phase == "extract":
                pct = 0.15 * (done / total)
                detail = f"extracting page {done}/{total}"
            elif phase == "ocr":
                pct = 0.15 + 0.60 * (done / total)
                detail = f"OCR page {done}/{total}"
            elif phase == "tables":
                pct = 0.75 + 0.25 * (done / total)
                detail = f"extracting tables {done}/{total}"
            else:
                continue
            await ingest_progress.mark_stage_progress(
                run_id=run_id, stage_pct=pct, stage_detail=detail,
            )

    _relay_task = asyncio.create_task(_relay_progress())
    wall_cap_s = _parse_wall_cap_s(pre.get("page_count"))
    try:
        # F12 (2026-08-11) — hard wall-clock cap on the parse subprocess.
        # It sits under the task's execution_timeout so a hung parser (e.g.
        # the §04p subprocess-pool instability on image-only PDFs) fails
        # from OUR side with a pool reset, letting Hatchet's retries=1
        # re-run against a fresh pool instead of the task dying opaquely at
        # the Hatchet timeout with a poisoned pool. Scaled by page count
        # since HAT-9; see _parse_wall_cap_s.
        result_dict = await asyncio.wait_for(
            loop.run_in_executor(
                pool,
                _run_parser_subprocess,
                body_path,
                pre.get("sha256", ""),
                _progress_path,
            ),
            timeout=wall_cap_s,
        )
    except TimeoutError as exc:
        _reset_parse_pool()
        if wall_cap_s > PARSE_WALL_CAP_BASE_S:
            # A document big enough to need the scaled budget and still
            # over it is slow, not hung. A retry starts from page 1 and
            # re-bills every OCR page for the same outcome, so stop here.
            log.error(
                "ingest_pdf.parse: subprocess exceeded its %ds page-scaled "
                "cap (page_count=%s) key=%s — pool reset, NOT retrying: a "
                "retry re-OCRs every page. Raise PDF_PARSE_SECONDS_PER_PAGE "
                "if this is a genuine throughput limit.",
                wall_cap_s, pre.get("page_count"), input.minio_key,
            )
            raise NonRetryableException(
                f"parse exceeded its {wall_cap_s}s page-scaled cap "
                f"(page_count={pre.get('page_count')}); not retried, a retry "
                "would re-OCR every page",
            ) from exc
        log.error(
            "ingest_pdf.parse: subprocess exceeded the %ds hard timeout "
            "key=%s — resetting parse pool and re-raising so Hatchet retries.",
            wall_cap_s, input.minio_key,
        )
        raise
    except BrokenProcessPool as exc:
        # 2026-05-23 — kill the in-process fallback. The original
        # fallback ran the parser on the default asyncio thread pool,
        # which:
        #   1. loaded heavy OCR/layout models in the SAME process that
        #      just got its subprocess OOM-killed — guaranteed to push
        #      memory over the edge again, often killing the whole
        #      worker (cf. [[tiff-smoke-2026-05-23]] root cause);
        #   2. blocked the Hatchet event loop on a multi-minute parse,
        #      starving heartbeats and getting the task re-queued by
        #      Hatchet's dead-worker detection.
        #
        # New behaviour: also rebuild the pool (the BrokenProcessPool
        # error means our cached pool is poisoned) and raise so Hatchet's
        # retries=1 backoff kicks in — by then memory pressure may have
        # eased. The per-workspace concurrency cap above means we won't
        # find ourselves in the same OOM situation on retry within the
        # same workspace.
        log.error(
            "ingest_pdf.parse: subprocess pool broken (%s) — likely OOM. "
            "Rebuilding pool; Hatchet will retry the step (retries=1). "
            "If retry also fails, raise PARSE_MIN_FREE_RAM_MB or reduce "
            "host memory pressure.", exc,
        )
        _reset_parse_pool()
        raise
    finally:
        _relay_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _relay_task
        with contextlib.suppress(Exception):
            os.unlink(_progress_path)
        # The parse task owns the body file, whether preflight
        # downloaded it or _resolve_body_path did. Deleting it here
        # rather than inside the subprocess covers the paths where the
        # subprocess frame never completes: the 3300 s hard timeout, a
        # BrokenProcessPool, and a cancellation from Hatchet. On an
        # 8 Gi worker with a small ephemeral disk, a handful of orphaned
        # 1.5 GB atlases is a full disk.
        with contextlib.suppress(Exception):
            os.unlink(body_path)
    return _pack_parse_output(ParseOut(**result_dict))


# ---- Step 3: persist ---------------------------------------------------------
# Mirrors v1.49 silver.reports INSERT (silver_reports.py:42-72) verbatim and
# also writes silver.shadow_runs + audit.audit_ledger.
INSERT_REPORT_SQL = """
INSERT INTO silver.reports (
    report_id, title, authors, company, filing_date, commodity,
    project_name, region, resource_estimate, sections_text,
    embedding_ids, parse_quality_pct, parser_used,
    is_scanned, source_file_sha256, project_id, workspace_id, page_count,
    extraction_confidence, source_object_key, text_page_coverage_pct,
    created_at, updated_at
)
VALUES (
    $1, $2, $3::text[], $4, $5::date, $6,
    $7, $8, $9::jsonb, $10::jsonb,
    ARRAY[]::text[], $11, $12,
    $13, $14, $16::uuid, $15::uuid, $17::int,
    $18::real, $19, $20::real,
    NOW(), NOW()
)
ON CONFLICT (report_id) DO UPDATE SET
    title          = EXCLUDED.title,
    authors        = EXCLUDED.authors,
    company        = EXCLUDED.company,
    filing_date    = EXCLUDED.filing_date,
    commodity      = EXCLUDED.commodity,
    project_name   = EXCLUDED.project_name,
    region         = EXCLUDED.region,
    resource_estimate = EXCLUDED.resource_estimate,
    sections_text  = EXCLUDED.sections_text,
    parser_used    = EXCLUDED.parser_used,
    parse_quality_pct = EXCLUDED.parse_quality_pct,
    is_scanned     = EXCLUDED.is_scanned,
    page_count     = EXCLUDED.page_count,
    extraction_confidence = EXCLUDED.extraction_confidence,
    -- The bronze object this row was parsed from. Needed by the Reader's
    -- side-by-side view: without it there is no link from a report back to
    -- the PDF whose pages the extracted text is supposed to correspond to.
    source_object_key = EXCLUDED.source_object_key,
    text_page_coverage_pct = EXCLUDED.text_page_coverage_pct,
    updated_at     = NOW()
"""

# Phase 1 R-P1-4 — write one passage per non-empty parsed section.
# UNIQUE (document_id, revision_number, text_hash) makes re-runs idempotent:
# the same parsed section yields the same text_hash; ON CONFLICT DO NOTHING
# ⇒ no duplicate passage on persist retry.
INSERT_PASSAGE_SQL = """
INSERT INTO silver.document_passages (
    document_id, workspace_id, revision_number,
    text, text_hash, ordinal, chunk_kind,
    page_first, page_last,
    ocr_confidence, ocr_method, ocr_status,
    created_at, updated_at
)
VALUES ($1, $2::uuid, 1, $3, $4, $5, 'narrative', $6, $7, $8, $9, $10, NOW(), NOW())
ON CONFLICT (document_id, revision_number, text_hash) DO UPDATE SET
    page_first     = COALESCE(EXCLUDED.page_first,     silver.document_passages.page_first),
    page_last      = COALESCE(EXCLUDED.page_last,      silver.document_passages.page_last),
    -- Phase 3 (2026-05-22): preserve existing OCR provenance on retry,
    -- only fill in if currently NULL. Avoids overwriting a real captured
    -- confidence with NULL on a Hatchet retry of the same parse.
    ocr_confidence = COALESCE(silver.document_passages.ocr_confidence, EXCLUDED.ocr_confidence),
    ocr_method     = COALESCE(silver.document_passages.ocr_method,     EXCLUDED.ocr_method),
    -- Preserve agent-driven states, but allow a fail-closed OCR assessment
    -- discovered on retry to promote an accepted passage to low_confidence.
    ocr_status     = CASE
        WHEN silver.document_passages.ocr_status = 'accepted'
         AND EXCLUDED.ocr_status = 'low_confidence'
        THEN EXCLUDED.ocr_status
        ELSE silver.document_passages.ocr_status
    END,
    updated_at     = NOW()
"""

# Multimodal page-image passages (2026-08-18). Separate statement from
# INSERT_PASSAGE_SQL because the column set genuinely differs (modality,
# page_number, image_object_key; no OCR provenance — a render has no OCR
# confidence) and folding both into one statement with a pile of NULLs
# obscured which columns each kind actually populates.
#
# chunk_kind='page_image' keeps these rows OUT of the narrative GC below,
# which deletes by (document_id, chunk_kind='narrative', stale hash). Image
# rows are re-derived from the page number, not from parsed text, so a
# re-parse that changes chunking must not delete them.
INSERT_IMAGE_PASSAGE_SQL = """
INSERT INTO silver.document_passages (
    document_id, workspace_id, revision_number,
    text, text_hash, ordinal, chunk_kind,
    page_first, page_last,
    modality, page_number, image_object_key,
    created_at, updated_at
)
VALUES ($1, $2::uuid, 1, $3, $4, $5, 'page_image', $6, $6,
        'image', $6, $7, NOW(), NOW())
-- Keyed on the PAGE, not the text hash (see migration
-- 2026_08_18_020000_key_image_passages_by_page_not_text). Verbalization
-- rewrites this row's text, changing its hash; a hash-keyed conflict target
-- would therefore miss on re-parse and create a second row for the same page.
ON CONFLICT (document_id, revision_number, page_number)
    WHERE modality = 'image'
DO UPDATE SET
    image_object_key = EXCLUDED.image_object_key,
    -- Deliberately NOT overwriting text/text_hash: if the verbalization
    -- sweep has already described this page, a re-parse must not throw that
    -- away and reinstate the placeholder. The description belongs to the
    -- page image, which has not changed.
    updated_at       = NOW()
"""

INSERT_OCR_REVIEW_SQL = """
INSERT INTO silver.review_queue (
    queue_id, workspace_id, project_id, target_table, target_record_kind,
    bronze_uri, bronze_row_offset, payload, confidence_per_field,
    confidence_record, parser_version, routing_decision, routing_reason,
    outlier_flags
)
SELECT
    $1::uuid, $2::uuid, $3::uuid, 'silver.document_passages', 'ocr_page',
    -- $4/$8 carry explicit ::text casts in BOTH positions: in INSERT..SELECT
    -- the target-list occurrence deduces `text` (no column context) while the
    -- WHERE comparison deduces `varchar` — Postgres refuses the prepare with
    -- AmbiguousParameterError ("inconsistent types deduced") unless unified.
    $4::text, NULL, $5::jsonb, $6::jsonb,
    $7, $8::text, 'review_required'::silver.review_routing_enum, $9,
    $10::jsonb
WHERE NOT EXISTS (
    SELECT 1
    FROM silver.review_queue existing
    WHERE existing.workspace_id = $2::uuid
      AND existing.project_id = $3::uuid
      AND existing.target_table = 'silver.document_passages'
      AND existing.target_record_kind = 'ocr_page'
      AND existing.bronze_uri = $4::text
      AND existing.payload->>'page_number' = $5::jsonb->>'page_number'
      AND existing.parser_version = $8::text
      AND existing.lifecycle IN ('pending', 'in_review')
)
ON CONFLICT (queue_id) DO NOTHING
"""


#: Routing decisions that produce a silver.review_queue row.
_QUEUED_ROUTING_DECISIONS = frozenset({"review_required", "spot_check"})


def _build_ocr_review_rows(
    parsed: dict,
    *,
    report_id: str,
    workspace_id: str,
    project_id: str,
    bronze_uri: str,
) -> list[dict]:
    """Translate parser assessments into the existing review-queue contract."""

    rows: list[dict] = []
    for warning in parsed.get("warnings") or []:
        if warning.get("code") != "ocr_quality_assessment":
            continue
        # spot_check pages are queued for a human sample too; only
        # review_required pages also demote their passages (see
        # _ocr_review_pages). The queue row's routing_decision column is
        # the enum value 'review_required' either way — the tier travels in
        # payload.ocr_quality_tier.
        if warning.get("routing_decision") not in _QUEUED_ROUTING_DECISIONS:
            continue

        try:
            page_number = int(warning.get("page") or 0)
        except (TypeError, ValueError):
            log.warning(
                "ingest_pdf.persist: ignored OCR review warning with invalid page=%r",
                warning.get("page"),
            )
            continue
        if page_number <= 0:
            continue
        signals = dict(warning.get("signals") or {})
        mean_confidence = max(
            0.0,
            min(1.0, float(signals.get("mean_confidence") or 0.0)),
        )
        reasons = [
            str(reason)
            for reason in (warning.get("reasons") or [])
            if str(reason).strip()
        ]
        tier = str(warning.get("tier") or "mandatory_review")
        ocr_method = str(warning.get("ocr_method") or "unknown")
        parser_version = (
            f"pdf_report:{warning.get('parser_version') or 'unknown'}:"
            f"{ocr_method}:ocr-quality-v1"
        )[:128]
        payload = {
            "document_id": report_id,
            "page_number": page_number,
            "page_first": page_number,
            "page_last": page_number,
            "text": str(warning.get("extracted_text") or ""),
            "ocr_method": ocr_method,
            "ocr_quality_tier": tier,
        }
        queue_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                (
                    f"georag:ocr-review:{workspace_id}:{project_id}:"
                    f"{bronze_uri}:{page_number}:{parser_version}"
                ),
            )
        )
        rows.append({
            "queue_id": queue_id,
            "workspace_id": workspace_id,
            "project_id": project_id,
            "bronze_uri": bronze_uri,
            "payload": payload,
            "confidence_per_field": signals,
            "confidence_record": mean_confidence,
            "parser_version": parser_version,
            "routing_reason": ", ".join(reasons)[:512] or tier,
            "outlier_flags": [
                {"field": "ocr_quality", "reason": reason}
                for reason in reasons
            ],
        })
    return rows


def _ocr_review_pages(parsed: dict) -> set[int]:
    """Pages whose passages persist ``ocr_status='low_confidence'``.

    Only ``review_required`` pages. ``spot_check`` pages get a review-queue
    row (see `_build_ocr_review_rows`) but stay ``accepted`` in retrieval.
    """

    pages: set[int] = set()
    for warning in parsed.get("warnings") or []:
        if warning.get("code") != "ocr_quality_assessment":
            continue
        if warning.get("routing_decision") != "review_required":
            continue
        try:
            page_number = int(warning.get("page") or 0)
        except (TypeError, ValueError):
            continue
        if page_number > 0:
            pages.add(page_number)
    return pages


def _ocr_status_for_section(section: dict, review_pages: set[int]) -> str:
    """Map page-level review routing onto the existing passage status enum."""

    page_first = int(section.get("page_first") or 0)
    page_last = int(section.get("page_last") or page_first)
    if page_first <= 0:
        return "accepted"
    return (
        "low_confidence"
        if any(page_first <= page <= page_last for page in review_pages)
        else "accepted"
    )


def _stable_report_id(
    *,
    workspace_id: str,
    project_id: str,
    source_identity: str,
) -> str:
    """Return a retry-stable report UUID for one project-scoped source."""

    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"georag:report:{workspace_id}:{project_id}:{source_identity}",
        )
    )


#: A document whose pages produced text on less than this fraction of them is
#: reported as ``partial``. 0.5 is the line pdf_report already draws when it
#: logs "this document is largely empty" (`text_page_coverage < 0.5`), taken
#: from the same `text_page_coverage_pct` it stores on silver.reports. The
#: column is NAMED `_pct` but holds a FRACTION (0..1), so this is 0.5, not 50.
MIN_TEXT_PAGE_COVERAGE = 0.5

#: When the remote engine could not answer for at least this fraction of the
#: pages it was asked about (and the native text layer stood in), the engine
#: itself is the problem -- a bad key, a throttle, an outage -- not one flaky
#: page. Below it the fallback is recorded as information only.
PARSE_FALLBACK_PARTIAL_FRACTION = 0.25

#: Pages named in a stored warning. A 400-page scan must not write a 400-entry
#: list into a jsonb column the UI prints.
_MAX_WARNING_PAGES = 25

#: OCR routing tiers that mean "a human should look at this page".
_OCR_REVIEW_TIERS = frozenset({"mandatory_review", "catastrophic_failure"})


def _page_list(pages: list[int]) -> list[int]:
    return sorted(set(pages))[:_MAX_WARNING_PAGES]


def _pages_phrase(pages: list[int]) -> str:
    unique = sorted(set(pages))
    shown = ", ".join(str(p) for p in unique[:_MAX_WARNING_PAGES])
    more = len(unique) - _MAX_WARNING_PAGES
    return f"{shown}{f' (+{more} more)' if more > 0 else ''}"


def build_run_warnings(
    parsed: dict[str, Any], *, passages_written: int, page_count: int,
) -> list[dict]:
    """Turn a parse result into the warnings a run row should carry.

    Before this, persist kept only ``warnings_count`` and called nothing
    else, so the Parse warnings (``ocr_page_budget_exhausted``, the parse-mode
    summary, ``page_ocr_recovered_fitz``, ``ocr_quality_assessment``,
    ``mixed_language_document``) never left the Hatchet run object, and a
    400-page scan whose OCR failed completed green with zero passages.

    The raw list is far too long to store (one ``ocr_quality_assessment`` per
    OCR'd page, each with a text excerpt), so entries are AGGREGATED per code
    with a ``count`` and at most :data:`_MAX_WARNING_PAGES` page numbers, and
    every entry has the ``detail`` string the Ingestion Runs page prints.

    ``severity: "info"`` entries are stored for a reader but do not make the
    run ``partial`` (see ``_progress.terminal_status``). Everything else does.
    Two conditions are synthesised because they are the cases where a run
    finished and delivered nothing useful: no text passage was written for a
    document that has pages (``no_text_passages``), and fewer than
    :data:`MIN_TEXT_PAGE_COVERAGE` of its pages produced text
    (``low_text_page_coverage``).
    """
    out: list[dict] = []

    def add(code: str, detail: str, *, severity: str = "warning", **extra: Any) -> None:
        out.append({"code": code, "severity": severity, "detail": detail[:500], **extra})

    pages_by_code: dict[str, list[int]] = {}
    review_pages: list[int] = []
    other_messages: dict[str, str] = {}
    other_counts: dict[str, int] = {}
    mode_summary: dict[str, Any] | None = None
    languages: list[str] = []
    budget: dict[str, Any] | None = None

    for raw in parsed.get("warnings") or []:
        if not isinstance(raw, dict):
            raw = {"message": str(raw)}
        code = str(raw.get("code") or "parse_warning")
        page = raw.get("page")
        if code == "ocr_quality_assessment":
            if str(raw.get("tier") or "") in _OCR_REVIEW_TIERS and isinstance(page, int):
                review_pages.append(page)
        elif code == "ocr_page_budget_exhausted":
            budget = raw
        elif code == "pdf_parse_mode_summary":
            mode_summary = raw
        elif code == "mixed_language_document":
            languages = list((raw.get("context") or {}).get("languages") or [])
        elif code in {
            "page_ocr_recovered_fitz",
            "page_parse_under_read",
            "pdf_extraction_partial",
            "page_ocr_exception_native_salvaged",
        }:
            pages_by_code.setdefault(code, [])
            if isinstance(page, int):
                pages_by_code[code].append(page)
            if raw.get("message"):
                other_messages.setdefault(code, str(raw["message"]))
        elif code in {
            "page_ocr_recovered", "page_short_text_salvaged",
            "two_column_layout_detected",
        } or raw.get("severity") == "info":
            continue  # benign, and per-page: not worth a row on the run
        else:
            # resource_table_extraction_failed, all_table_extraction_failed,
            # remote_table_pass_failed, preflight leftovers, anything new:
            # unknown means "tell someone", once per code.
            other_counts[code] = other_counts.get(code, 0) + 1
            other_messages.setdefault(code, str(raw.get("message") or code))

    for pw in parsed.get("page_image_warnings") or []:
        if isinstance(pw, dict) and pw.get("code"):
            out.append({
                **pw,
                "severity": pw.get("severity") or "warning",
                "detail": str(pw.get("detail") or pw["code"])[:500],
            })

    if budget is not None:
        add(
            "ocr_page_budget_exhausted",
            str(budget.get("message") or "Remote OCR page budget exhausted."),
            cap=budget.get("cap"),
        )

    if review_pages:
        add(
            "ocr_pages_need_review",
            f"OCR quality is below the acceptance bar on {len(set(review_pages))} "
            f"page(s) (review required): {_pages_phrase(review_pages)}.",
            count=len(set(review_pages)),
            pages=_page_list(review_pages),
        )

    under = pages_by_code.get("page_parse_under_read", [])
    if under:
        add(
            "page_parse_under_read",
            f"Cohere Parse returned markedly less text than the PDF's own text "
            f"layer on {len(set(under))} page(s), so the text layer was kept "
            f"(figure labels and other non-text content may be missing from "
            f"Parse's reading): {_pages_phrase(under)}.",
            count=len(set(under)),
            pages=_page_list(under),
        )

    broken = pages_by_code.get("pdf_extraction_partial", [])
    if "pdf_extraction_partial" in pages_by_code:
        add(
            "pdf_extraction_partial",
            f"Text extraction raised on {max(len(set(broken)), 1)} page(s)"
            + (f": {_pages_phrase(broken)}" if broken else "")
            + (f" ({other_messages['pdf_extraction_partial']})"
               if other_messages.get("pdf_extraction_partial") else "")
            + ".",
            count=len(set(broken)),
            pages=_page_list(broken),
        )

    salvaged = pages_by_code.get("page_ocr_exception_native_salvaged", [])
    if "page_ocr_exception_native_salvaged" in pages_by_code:
        add(
            "page_ocr_exception_native_salvaged",
            f"OCR raised on {len(set(salvaged))} page(s); their short native "
            f"text was kept: {_pages_phrase(salvaged)}.",
            count=len(set(salvaged)),
            pages=_page_list(salvaged),
        )

    for code, n in other_counts.items():
        add(code, other_messages[code], count=n)

    if mode_summary is not None:
        fallback = [int(x) for x in mode_summary.get("native_fallback_pages") or []]
        answered = [int(x) for x in mode_summary.get("engine_text_pages") or []]
        asked = len(fallback) + len(answered)
        degraded = bool(asked) and len(fallback) / asked >= PARSE_FALLBACK_PARTIAL_FRACTION
        add(
            "parse_engine_degraded" if degraded else "pdf_parse_mode_summary",
            (
                f"PDF_PARSE_MODE={mode_summary.get('mode')}: Cohere Parse could not "
                f"answer for {len(fallback)} of {asked} text-layer page(s); their "
                f"native text layer was used (no Parse tables or figure reading)."
                if degraded else
                f"PDF_PARSE_MODE={mode_summary.get('mode')}: Parse read "
                f"{len(answered)} page(s); {len(fallback)} kept their native text."
            ),
            severity="warning" if degraded else "info",
            engine_pages=len(answered),
            native_fallback_count=len(fallback),
            native_fallback_pages=_page_list(fallback),
        )

    recovered = pages_by_code.get("page_ocr_recovered_fitz", [])
    if recovered:
        add(
            "page_ocr_recovered_fitz",
            f"OCR recovered text on {len(set(recovered))} page(s) that had no "
            f"usable text layer.",
            severity="info",
            count=len(set(recovered)),
        )

    if languages:
        add(
            "mixed_language_document",
            "Mixed-language document ("
            + ", ".join(str(x) for x in languages)
            + "); Tesseract reads fallback pages with English plus the dominant "
            "non-English language when that data is installed, otherwise English only.",
            severity="info",
            languages=languages,
        )

    coverage = parsed.get("text_page_coverage_pct")
    if page_count > 0 and passages_written == 0:
        add(
            "no_text_passages",
            f"No text passages were written for this {page_count}-page document "
            f"(text found on {float(coverage or 0.0) * 100:.0f}% of pages). "
            f"Nothing from it can be retrieved as text: check the OCR engine "
            f"and COHERE_API_KEY, then re-ingest.",
            pages=page_count,
            text_page_coverage=float(coverage or 0.0),
        )
    elif page_count > 0 and coverage is not None and float(coverage) < MIN_TEXT_PAGE_COVERAGE:
        add(
            "low_text_page_coverage",
            f"Only {float(coverage) * 100:.0f}% of this document's {page_count} "
            f"pages produced text (minimum {MIN_TEXT_PAGE_COVERAGE * 100:.0f}%); "
            f"the rest were blank, figure-only or failed OCR.",
            pages=page_count,
            text_page_coverage=float(coverage),
        )

    return out


@ingest_pdf.task(
    execution_timeout="15m", schedule_timeout="2h", retries=2, parents=[parse],
    backoff_factor=8.0, backoff_max_seconds=60,
)
async def persist(input: IngestPdfInput, ctx: Context) -> IngestPdfFinalOut:
    """Write silver.reports + silver.shadow_runs + audit.audit_ledger."""
    if input.project_id and input.workspace_id:
        await ingest_progress.mark_started(
            workspace_id=str(input.workspace_id),
            project_id=str(input.project_id),
            minio_key=input.minio_key,
            step="persist",
            run_id=input.run_id,
        )
    # Reliability spec Fix 1d — keep last_heartbeat_at fresh while the
    # potentially-slow persist transaction runs.
    async with ingest_progress.heartbeat_loop(
        run_id=input.run_id,
        workspace_id=str(input.workspace_id),
        minio_key=input.minio_key,
    ):
        return await _persist_body(input, ctx)


async def _persist_preflight_rejection(
    input: IngestPdfInput,
    *,
    pre: dict[str, Any],
    parsed: dict[str, Any],
) -> IngestPdfFinalOut:
    """Close the run as failed and write NO report row.

    Extracted from _persist_body 2026-08-21 (L1097). It was the one phase
    of that 695-line function with no shared mutable state, and the phase
    whose failure had already cost something.

    2026-08-14 — preflight rejection must FAIL the run, not persist a
    phantom report. parse() short-circuits with parser_used="skipped" when
    preflight marked the file invalid (password-protected, missing %PDF-
    magic, >2GB, inactive project). The old code fell through to the normal
    persist path: an empty "(untitled)" silver.reports row landed,
    embed_verify then saw zero unembedded passages and marked the run
    COMPLETED — the rejection was invisible to the user. Now: mark the run
    failed with the preflight error (which reaches
    silver.ingest_progress.error_text and so the IngestionRuns UI), write
    no report row, and return a skipped final output.

    Note the asymmetry this function has to carry: the WORKFLOW succeeds
    here. Returning a value rather than raising is deliberate — a rejected
    upload is not a system fault and must not burn Hatchet retries — but it
    means the on_failure hook never fires, so the terminal broadcast below
    is the only thing that flips the UI without waiting for its poll.
    """
    preflight_error = pre.get("error") or next(
        (
            w.get("message")
            for w in (parsed.get("warnings") or [])
            if w.get("code") == "preflight_rejected" and w.get("message")
        ),
        "preflight rejected the file",
    )
    log.warning(
        "ingest_pdf.persist: preflight rejected key=%s (%s) — failing "
        "run, no report row written",
        input.minio_key, preflight_error,
    )

    run_id: str | None = None
    if input.workspace_id:
        run_id = await ingest_progress.lookup_active_run_id(
            workspace_id=str(input.workspace_id),
            minio_key=input.minio_key,
        )
    if run_id:
        await ingest_progress.mark_failed_by_run(
            run_id=run_id,
            stage="preflight",
            error=f"preflight_rejected: {preflight_error}",
        )
        if input.project_id:
            try:
                from app.services.laravel_bridge import post_ingestion_progress  # noqa: PLC0415
                await post_ingestion_progress(
                    workspace_id=str(input.workspace_id),
                    project_id=str(input.project_id),
                    run_id=run_id,
                    stage="preflight",
                    status="failed",
                    message=f"Upload rejected: {preflight_error}",
                )
            except Exception as exc:  # noqa: BLE001 — best-effort broadcast
                # A broadcast failure must not turn a clean rejection into
                # a workflow error. The row is already terminal; the UI
                # picks it up on its next poll.
                log.warning(
                    "ingest_pdf.persist: preflight-rejection broadcast "
                    "failed run=%s: %s", run_id, exc,
                )

    return IngestPdfFinalOut(
        sha256=pre.get("sha256", ""),
        parser_used="skipped",
        parse_quality_pct=0.0,
        page_count=int(pre.get("page_count", 0) or 0),
        title=None,
        authors=[],
        company=None,
        filing_date=None,
        commodity=None,
        project_name=None,
        region=None,
        sections_count=0,
        resource_tables_count=0,
        is_scanned=False,
        warnings_count=len(parsed.get("warnings") or []),
        parse_duration_ms=0,
        persist_duration_ms=0,
        report_id=None,
        passages_written=0,
    )


async def _persist_body(input: IngestPdfInput, ctx: Context) -> IngestPdfFinalOut:
    """Inner body of persist() — wrapped so heartbeat_loop can manage the
    ticker around the slow §04p dual-write + Postgres transaction."""
    pre = ctx.task_output(preflight)
    pre = pre.model_dump() if hasattr(pre, "model_dump") else dict(pre)
    parsed = ctx.task_output(parse)
    parsed = parsed.model_dump() if hasattr(parsed, "model_dump") else dict(parsed)
    # parse packs an output too big for a task output (see _pack_parse_output).
    parsed = _unpack_parse_output(parsed)

    # Preflight rejection is its own phase and lives in its own function
    # (L1097). parse() short-circuits with parser_used="skipped" when
    # preflight marked the file invalid; everything below assumes a real
    # parse result.
    if (parsed.get("parser_used") or "") == "skipped":
        return await _persist_preflight_rejection(input, pre=pre, parsed=parsed)

    t_start = time.monotonic()
    report_id = _stable_report_id(
        workspace_id=str(input.workspace_id or LEGACY_DEFAULT_TENANT_UUID),
        project_id=str(input.project_id),
        source_identity=str(pre.get("sha256") or input.minio_key),
    )

    # Build resource_estimate payload to match v1.49 exactly.
    resource_estimate: dict = {}
    if parsed.get("resource_tables"):
        resource_estimate["pdfplumber_v1"] = {
            "tables": parsed["resource_tables"],
            "source": "pdfplumber_v1",
        }

    # Figure manifest consumption. If the parse task's figure_manifest
    # producer uploaded a figure to figures/_pending/{sha}/figure_
    # {idx:04d}_page_{n}.png and returned a manifest entry in
    # ParseOut.figures, this renames the PNG to its final
    # figures/{report_id}/... key (S3 copy+delete) and creates one
    # ReportSection per figure so the caption text is chunked + embedded
    # alongside narrative sections (caption hits in chat surface the
    # figure citation).
    #
    # Currently a no-op in practice: docling, the previous figure_manifest
    # producer, was removed 2026-07-29 (it never ran in production —
    # PDF_PARSER_DOCLING_ENABLED was false in every live deployment), and
    # parse_pdf_report always returns figure_manifest=[] now. This block
    # stays wired up for a future producer (see
    # app.services.ingest.pdf_report.ReportParseResult.figure_manifest and
    # app.agent.figure_extractor for candidates).
    figure_manifest_final: list[dict] = []
    pending_manifest = parsed.get("figures") or []
    if pending_manifest:
        try:
            store = get_storage_client()

            figure_sections_out: list[dict] = []
            for entry in pending_manifest:
                idx = entry.get("idx")
                page_no = entry.get("page")
                caption = (entry.get("caption") or "").strip()
                pending_key = entry.get("pending_key")
                # entry.get("bucket") is ignored on purpose — pending figure
                # uploads always land in the bronze bucket, so Bucket.BRONZE
                # is always correct here.
                img_sha = entry.get("sha256")

                final_key = None
                if pending_key:
                    final_key = (
                        f"figures/{report_id}/"
                        f"figure_{int(idx):04d}_page_{page_no}.png"
                    )
                    try:
                        # Audit 2026-06-27 (T4, hard rule 2): boto3 is sync; this
                        # persist step runs on the Hatchet worker's asyncio loop,
                        # so the S3 round-trips must go off-loop via to_thread or
                        # they block heartbeats + other tasks. (The download path
                        # at the top of this file already uses the async client.)
                        await asyncio.to_thread(
                            store.copy,
                            Bucket.BRONZE,
                            pending_key,
                            Bucket.BRONZE,
                            final_key,
                            metadata={
                                "report_id": str(report_id),
                                "project_id": (
                                    str(input.project_id) if input.project_id else ""
                                ),
                                "page": str(page_no),
                                "sha256": str(img_sha or ""),
                            },
                            content_type="image/png",
                        )
                        try:
                            await asyncio.to_thread(store.delete, Bucket.BRONZE, pending_key)
                        except Exception as del_exc:  # noqa: BLE001
                            log.warning(
                                "ingest_pdf.persist: pending figure delete "
                                "failed (key=%s): %s",
                                pending_key, del_exc,
                            )
                    except Exception as copy_exc:  # noqa: BLE001
                        log.warning(
                            "ingest_pdf.persist: figure copy failed "
                            "(pending=%s → final=%s): %s",
                            pending_key, final_key, copy_exc,
                        )
                        final_key = None

                # Phase 2: content-aware caption from the Qwen3-VL sidecar,
                # folded into the section text so it's embedded with the figure.
                # Flag-gated + best-effort: any failure keeps the manifest's
                # own caption text.
                vl_desc: str | None = None
                if _FIGURE_VL_CAPTIONS and final_key:
                    try:
                        from app.agent.figure_extractor import caption_image_with_vl
                        _img = await asyncio.to_thread(store.get_bytes, Bucket.BRONZE, final_key)
                        vl_desc = await caption_image_with_vl(
                            _img, context=parsed.get("title"),
                        )
                    except Exception as vl_exc:  # noqa: BLE001 — never block persist
                        log.warning(
                            "ingest_pdf.persist: figure VL caption failed (key=%s): %s",
                            final_key, vl_exc,
                        )

                section_lines = [f"Figure on page {page_no}."]
                if caption:
                    section_lines.append(f"Caption: {caption}")
                if vl_desc:
                    section_lines.append(f"Description: {vl_desc}")
                if final_key:
                    section_lines.append(f"Image: s3://{Bucket.BRONZE.value}/{final_key}")
                figure_sections_out.append({
                    "section_number": None,
                    "section_title": f"Figure (page {page_no}, #{int(idx) + 1})",
                    "text": "\n".join(section_lines),
                    "page_first": page_no,
                    "page_last": page_no,
                })

                figure_manifest_final.append({
                    "idx": idx,
                    "page": page_no,
                    "bbox": entry.get("bbox"),
                    "caption": caption,
                    "minio_key": final_key,
                    "sha256": img_sha,
                })

            if figure_sections_out:
                log.info(
                    "ingest_pdf.persist: registered %d figure section(s) for "
                    "report=%s (uploaded=%d)",
                    len(figure_sections_out),
                    report_id,
                    sum(1 for m in figure_manifest_final if m.get("minio_key")),
                )
                parsed.setdefault("sections", []).extend(figure_sections_out)
        except Exception as fig_err:  # noqa: BLE001
            log.warning("ingest_pdf.persist: figure manifest consumption failed: %s", fig_err)

    # Surface the final figure manifest in resource_estimate so the UI /
    # future query layer can render figures inline with citations.
    if figure_manifest_final:
        resource_estimate["figures"] = {
            "items": figure_manifest_final,
            "source": "figure_manifest_v1",
        }

    # Build sections_text dict (v1.49 _build_sections_dict shape).
    # Collisions are disambiguated with the chunk's ordinal (matching the
    # silver.document_passages ordinal below) — the old single "_dup"
    # fallback kept only 2 of N chunks per section number.
    sections_text: dict = {}
    for ordinal, s in enumerate(parsed.get("sections", []) or []):
        n = s.get("section_number")
        title = (s.get("section_title") or "")
        key = str(n) if n is not None else (title.lower() or "section")
        if key in sections_text:
            key = f"{key}#{ordinal}"
        sections_text[key] = s.get("text") or ""

    title = parsed.get("title") or "(untitled)"  # silver.reports.title is NOT NULL

    # filing_date arrives as an ISO 8601 string from the parser; asyncpg
    # binds Python `date` to a Postgres `date` column.
    filing_date_raw = parsed.get("filing_date")
    filing_date_obj = None
    if filing_date_raw:
        from datetime import date as _date
        try:
            filing_date_obj = _date.fromisoformat(filing_date_raw[:10])
        except Exception:
            filing_date_obj = None

    # Hatchet step-boundary deserialization can drop UUID fields if the
    # input model has been re-validated upstream. Fall back to the bronze-
    # manifest workspace_id when input.workspace_id arrives None.
    workspace_id_str: str
    if input.workspace_id:
        workspace_id_str = str(input.workspace_id)
    else:
        # Recover from the bronze manifest or default workspace.
        # Audit item B4 — centralised legacy default + metric so Phase-2
        # cutover (raise instead of fallback) sees this as a single search.
        workspace_id_str = LEGACY_DEFAULT_TENANT_UUID
        with contextlib.suppress(Exception):
            WORKSPACE_RESOLUTION_FAILURES.labels(
                site="ingest_pdf.persist"
            ).inc()
        log.warning(
            "ingest_pdf.persist: input.workspace_id was null; "
            "falling back to default workspace. minio_key=%s",
            input.minio_key,
        )

    ocr_review_rows = _build_ocr_review_rows(
        parsed,
        report_id=report_id,
        workspace_id=workspace_id_str,
        project_id=str(input.project_id),
        bronze_uri=f"s3://{_bronze_bucket_name()}/{input.minio_key}",
    )
    ocr_review_pages = _ocr_review_pages(parsed)

    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2, statement_cache_size=0)
    try:
        async with pool.acquire() as conn:
            # --- silver.reports + silver.document_passages (atomic) ---
            # 2026-05-22: merged the previously-separate report and passage
            # transactions into ONE transaction. Before this, big PDFs that
            # exceeded the persist task's 2-minute execution_timeout during
            # the slow §04p dual-write left silver.reports rows with ZERO
            # passages — the user's "437 KB sections_text, 0 chunks" bug.
            # With a single transaction, either the whole report + all its
            # passages land together, or neither does and Hatchet retries.
            passages_written = 0
            # 2026-08-14 stale-passage GC — text_hashes of every passage the
            # CURRENT parse produced, plus the rows the GC DELETE removed
            # (their Qdrant points are deleted after the transaction commits).
            current_text_hashes: list[str] = []
            stale_passage_rows: list = []
            # Page-image bookkeeping. `_pending_page_keys` are the staged
            # renders whose copy to the final key succeeded; they are deleted
            # AFTER the transaction commits (never inside it: persist has
            # retries=2, and a rollback followed by a retry needs the pending
            # object to still exist to copy again). `_image_failed_pages` are
            # pages that lost their image here, reported on the run.
            _pending_page_keys: list[str] = []
            _image_failed_pages: list[int] = []
            _img_store = None
            # The run's verdict, built INSIDE the transaction (below) so it
            # commits with the passages it describes.
            run_warnings: list[dict] = []
            _progress_stamped = False
            async with conn.transaction():
                await bind_workspace_scope(
                    conn, workspace_id=workspace_id_str, site="hatchet.ingest_pdf"
                )
                await conn.execute(
                    INSERT_REPORT_SQL,
                    report_id,
                    title,
                    parsed.get("authors") or [],
                    parsed.get("company"),
                    filing_date_obj,
                    parsed.get("commodity"),
                    parsed.get("project_name"),
                    parsed.get("region"),
                    json.dumps(resource_estimate),
                    json.dumps(sections_text),
                    float(parsed.get("parse_quality_pct", 0.0) or 0.0),
                    (parsed.get("parser_used") or "unknown")[:30],
                    bool(parsed.get("is_scanned", False)),
                    pre.get("sha256"),
                    workspace_id_str,
                    str(input.project_id) if input.project_id else None,
                    int(pre.get("page_count", 0) or 0),
                    # $18 — see ReportParseResult.extraction_confidence. This
                    # column was in the table from the start but absent from
                    # this INSERT, so it read NULL on every production row and
                    # the OCR review-routing signal never had input. None is
                    # preserved rather than coerced to 0.0 so "not computed"
                    # stays distinguishable from "computed as zero confidence".
                    (
                        float(parsed["extraction_confidence"])
                        if parsed.get("extraction_confidence") is not None
                        else None
                    ),
                    # $19 — the bronze key this document was parsed from.
                    # Persisted so the Reader can put the original page next
                    # to the text extracted from it; previously minio_key was
                    # only ever a workflow input and nothing on the produced
                    # row pointed back at its source.
                    input.minio_key,
                    # $20 -- the fraction of pages that produced any text.
                    # parse_quality_pct sitting alone on this row is what
                    # let a report whose 300 pages OCR'd to nothing read
                    # as well parsed, because its table of contents
                    # yielded 17 NI 43-101 headings.
                    float(parsed.get("text_page_coverage_pct", 0.0) or 0.0),
                )
                for ordinal, section in enumerate(parsed.get("sections") or []):
                    text = (section.get("text") or "").strip()
                    if not text:
                        continue
                    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    current_text_hashes.append(text_hash)
                    # Phase 3 (2026-05-22) — OCR provenance. Default to
                    # None when the parser didn't supply values (e.g. older
                    # parsers or sections built outside _assign_ocr_metadata).
                    ocr_conf_raw = section.get("ocr_confidence")
                    ocr_method = section.get("ocr_method")
                    ocr_conf = float(ocr_conf_raw) if ocr_conf_raw is not None else None
                    status = await conn.execute(
                        INSERT_PASSAGE_SQL,
                        report_id,
                        workspace_id_str,
                        text,
                        text_hash,
                        ordinal,
                        section.get("page_first"),
                        section.get("page_last"),
                        ocr_conf,
                        ocr_method,
                        _ocr_status_for_section(section, ocr_review_pages),
                    )
                    if status.endswith(" 1"):
                        passages_written += 1

                # Page-image passages. The renders were staged under
                # _pending keys by the parse task (report_id wasn't known
                # yet); rename each into place, then write its row.
                #
                # Fail-soft per page, deliberately: a page whose copy or
                # insert fails is skipped with a warning rather than
                # aborting the transaction. The text passages above are the
                # product; image passages are additive coverage, and losing
                # one page's render must not cost the document its text.
                _page_images = parsed.get("page_image_manifest") or []
                if _page_images:
                    from app.services.ingest.page_image import (
                        final_key as _page_final_key,
                    )
                    from app.services.ingest.page_image import (
                        placeholder_text as _page_placeholder_text,
                    )

                    # Deliberately NOT reusing the `store` above: that name is
                    # bound inside `if pending_manifest:`, and the figure
                    # manifest is unconditionally empty (docling removed
                    # 2026-07-29), so `store` is never actually assigned on
                    # any live run. Depending on it would NameError on the
                    # first document with page images.
                    _img_store = get_storage_client()
                    _images_written = 0
                    for entry in _page_images:
                        page_no = entry.get("page_number")
                        pending = entry.get("pending_key")
                        if not page_no or not pending:
                            continue
                        dest = _page_final_key(str(report_id), int(page_no))
                        try:
                            await asyncio.to_thread(
                                _img_store.copy,
                                Bucket.BRONZE_RASTER,
                                pending,
                                Bucket.BRONZE_RASTER,
                                dest,
                                metadata={
                                    "report_id": str(report_id),
                                    "page": str(page_no),
                                },
                                content_type="image/png",
                            )
                        except Exception as _copy_exc:  # noqa: BLE001
                            # A persist RETRY after a commit finds the pending
                            # object already deleted (it is removed post-commit),
                            # so EVERY copy fails even though every page is
                            # already in place under its final key - which used
                            # to be reported as page_image_persist_failed for
                            # the whole document. The destination existing IS
                            # the copy having happened.
                            _dest_present = False
                            try:
                                _dest_present = bool(await asyncio.to_thread(
                                    _img_store.exists, Bucket.BRONZE_RASTER, dest,
                                ))
                            except Exception as _exists_exc:  # noqa: BLE001
                                log.warning(
                                    "ingest_pdf: page-image existence check failed "
                                    "page=%s key=%s err=%s", page_no, dest, _exists_exc,
                                )
                            if not _dest_present:
                                log.warning(
                                    "ingest_pdf: page-image copy failed page=%s "
                                    "key=%s err=%s", page_no, pending, _copy_exc,
                                )
                                _image_failed_pages.append(int(page_no))
                                continue
                            log.info(
                                "ingest_pdf: page-image copy failed (%s) but %s "
                                "already exists - treating page %s as finalised "
                                "(retry after commit)", _copy_exc, dest, page_no,
                            )
                        # The copy landed: the pending object has done its
                        # job. Deleted post-commit (see _pending_page_keys).
                        _pending_page_keys.append(pending)

                        _img_text = _page_placeholder_text(
                            int(page_no), parsed.get("title"),
                        )
                        # HAT-10 (2026-09-29): a SAVEPOINT per row. This
                        # loop runs inside persist's single transaction, and
                        # a failed statement aborts a Postgres transaction:
                        # without the savepoint the `continue` below carried
                        # on into InFailedSQLTransactionError on every later
                        # statement, and persist (retries=2) lost the whole
                        # document, the opposite of "fail-soft per page".
                        try:
                            async with conn.transaction():
                                _img_status = await conn.execute(
                                    INSERT_IMAGE_PASSAGE_SQL,
                                    report_id,
                                    workspace_id_str,
                                    _img_text,
                                    hashlib.sha256(_img_text.encode("utf-8")).hexdigest(),
                                    # $5 ordinal and $6 page number are both
                                    # the page: an image passage's position
                                    # in the document IS its page, unlike a
                                    # text chunk whose ordinal counts sections.
                                    int(page_no),
                                    int(page_no),
                                    dest,
                                )
                        except Exception as _img_exc:  # noqa: BLE001
                            log.warning(
                                "ingest_pdf: page-image row insert failed "
                                "page=%s err=%s", page_no, _img_exc,
                            )
                            _image_failed_pages.append(int(page_no))
                            continue
                        if _img_status.endswith(" 1"):
                            _images_written += 1

                    log.info(
                        "ingest_pdf: wrote %d page-image passages for report %s",
                        _images_written, report_id,
                    )

                for review_row in ocr_review_rows:
                    await conn.execute(
                        INSERT_OCR_REVIEW_SQL,
                        review_row["queue_id"],
                        review_row["workspace_id"],
                        review_row["project_id"],
                        review_row["bronze_uri"],
                        json.dumps(review_row["payload"]),
                        json.dumps(review_row["confidence_per_field"]),
                        review_row["confidence_record"],
                        review_row["parser_version"],
                        review_row["routing_reason"],
                        json.dumps(review_row["outlier_flags"]),
                    )

                # 2026-08-14 — GC passages superseded by this re-parse.
                # INSERT_PASSAGE_SQL upserts by (document_id,
                # revision_number, text_hash) but never deleted old rows:
                # when a chunking/OCR change altered a chunk's text, the
                # OLD chunk (and its Qdrant point) stayed retrievable
                # forever. Delete rows of THIS document whose hash the new
                # parse no longer produced. Guards:
                #   - only when the new parse yielded >0 passages, so a
                #     degenerate empty parse can't wipe a good document;
                #   - chunk_kind='narrative' only — the only kind this
                #     writer produces; 'section'/'paragraph' parent-child
                #     rows from other pipelines are untouched.
                # The matching Qdrant points (ids are passage UUIDs) are
                # deleted after the transaction commits, below.
                if current_text_hashes:
                    stale_passage_rows = await conn.fetch(
                        """
                        DELETE FROM silver.document_passages
                        WHERE document_id = $1
                          AND revision_number = 1
                          AND chunk_kind = 'narrative'
                          AND text_hash <> ALL($2::text[])
                        RETURNING passage_id::text AS passage_id, embedding_id
                        """,
                        report_id,
                        current_text_hashes,
                    )
                    if stale_passage_rows:
                        log.info(
                            "ingest_pdf.persist: deleted %d stale passage "
                            "row(s) for report=%s (superseded by re-parse)",
                            len(stale_passage_rows), report_id,
                        )

                # The run's verdict is written IN THIS TRANSACTION. Stamped
                # after the commit (as it used to be) there was a window in
                # which the passages were visible, a sweep that found them all
                # embedded closed the run with no stored diagnostics, and a
                # document whose OCR had failed finished as a bare
                # 'completed'. Both helpers write in a savepoint, so a failed
                # bookkeeping UPDATE cannot abort the document's write; if one
                # fails the post-commit fallback below retries it.
                run_warnings = build_run_warnings(
                    parsed,
                    passages_written=passages_written,
                    page_count=int(pre.get("page_count", 0) or 0),
                )
                if _image_failed_pages:
                    _failed = sorted(set(_image_failed_pages))
                    run_warnings.append({
                        "code": "page_image_persist_failed",
                        "severity": "warning",
                        "detail": (
                            f"{len(_failed)} page image(s) could not be finalised "
                            f"(copy or row insert failed): "
                            f"{', '.join(str(p) for p in _failed[:25])}"
                            f"{' ...' if len(_failed) > 25 else ''}. Their text is "
                            f"unaffected."
                        ),
                        "count": len(_failed),
                        "pages": _failed[:25],
                    })
                if input.workspace_id:
                    _report_stamped = await ingest_progress.mark_report_id(
                        workspace_id=str(input.workspace_id),
                        minio_key=input.minio_key,
                        report_id=report_id,
                        conn=conn,
                    )
                    _diagnostics_stamped = await ingest_progress.mark_run_diagnostics(
                        workspace_id=str(input.workspace_id),
                        minio_key=input.minio_key,
                        rows_written=passages_written,
                        warnings=run_warnings,
                        conn=conn,
                    )
                    _progress_stamped = bool(_report_stamped and _diagnostics_stamped)

            # The transaction committed: the finalised page images are the
            # only copy that matters now. Delete the _pending renders, the way
            # the figure path does. Without this every document left one PNG
            # per page under page-images/_pending/ for good (a 400-page report
            # at scope=all is 400 orphaned objects, and a re-ingest of the
            # same bytes only overwrites the same keys). Best-effort: a failed
            # delete costs storage, never the document.
            if _pending_page_keys and _img_store is not None:
                _deleted = 0
                for _pk in _pending_page_keys:
                    try:
                        await asyncio.to_thread(
                            _img_store.delete, Bucket.BRONZE_RASTER, _pk,
                        )
                        _deleted += 1
                    except Exception as _del_exc:  # noqa: BLE001
                        log.warning(
                            "ingest_pdf: pending page-image delete failed "
                            "key=%s err=%s", _pk, _del_exc,
                        )
                log.info(
                    "ingest_pdf: deleted %d/%d pending page-image render(s) "
                    "for report %s", _deleted, len(_pending_page_keys), report_id,
                )

            # silver.shadow_runs was dropped in Phase 4 Step 6 (sunset of the
            # v1.49 shadow-diff harness). The persist step previously
            # INSERTed a row here; that block is removed. `final.shadow_runs_id`
            # stays as the model default (None) for backward-compat with any
            # downstream consumer reading the field.

            persist_ms = int((time.monotonic() - t_start) * 1000)
            final = IngestPdfFinalOut(
                sha256=pre.get("sha256", ""),
                parser_used=parsed.get("parser_used") or "unknown",
                parse_quality_pct=float(parsed.get("parse_quality_pct", 0.0) or 0.0),
                page_count=int(pre.get("page_count", 0) or 0),
                title=parsed.get("title"),
                authors=list(parsed.get("authors") or []),
                company=parsed.get("company"),
                filing_date=parsed.get("filing_date"),
                commodity=parsed.get("commodity"),
                project_name=parsed.get("project_name"),
                region=parsed.get("region"),
                sections_count=len(parsed.get("sections") or []),
                resource_tables_count=len(parsed.get("resource_tables") or []),
                is_scanned=bool(parsed.get("is_scanned", False)),
                warnings_count=len(parsed.get("warnings") or []),
                parse_duration_ms=int(parsed.get("parse_duration_ms", 0) or 0),
                persist_duration_ms=persist_ms,
                report_id=report_id,
                passages_written=passages_written,
                run_warnings=run_warnings,
            )

            # --- audit.audit_ledger ---
            # Two action_types per run, matching Phase 1 §10.3:
            #   - ingest_pdf.parse.complete   (the parse stage)
            #   - silver.reports.write        (the row insert)
            # The diff classifier's CRITICAL_ACTION_TYPES check requires
            # both to be present on each side; emitting them here closes
            # R-P1-1 from the Phase 1 handoff.
            try:
                await emit_audit(
                    conn,
                    action_type="ingest_pdf.parse.complete",
                    workspace_id=input.workspace_id,
                    actor_id=input.actor_id,
                    actor_kind="workflow",
                    target_schema="silver",
                    target_table="reports",
                    target_id=report_id,
                    payload={
                        "minio_key": input.minio_key,
                        "sha256": final.sha256,
                        "parser_used": final.parser_used,
                        "parse_quality_pct": final.parse_quality_pct,
                        "page_count": final.page_count,
                        "sections_count": final.sections_count,
                        "resource_tables_count": final.resource_tables_count,
                        "is_scanned": final.is_scanned,
                        "warnings_count": final.warnings_count,
                        "parse_duration_ms": final.parse_duration_ms,
                        "persist_duration_ms": final.persist_duration_ms,
                        "report_id": report_id,
                        "shadow_runs_id": None,
                        "passages_written": final.passages_written,
                    },
                    trace_id=ctx.workflow_run_id,
                )
                await emit_audit(
                    conn,
                    action_type="silver.reports.write",
                    workspace_id=input.workspace_id,
                    actor_id=input.actor_id,
                    actor_kind="workflow",
                    target_schema="silver",
                    target_table="reports",
                    target_id=report_id,
                    payload={
                        "minio_key": input.minio_key,
                        "sha256": final.sha256,
                        "report_id": report_id,
                        "title": final.title,
                        "company": final.company,
                        "filing_date": final.filing_date,
                        "side": "hatchet",
                    },
                    trace_id=ctx.workflow_run_id,
                )
            except Exception as e:
                log.warning("audit emit failed: %s", e)
    finally:
        await pool.close()

    # 2026-08-14 — delete the Qdrant points of GC'd stale passages. Point
    # ids are the passage UUIDs (see passage_embedder._passage_to_point_id).
    # Runs AFTER the PG transaction committed so a rollback can't orphan
    # live rows' points; failure here is soft (rows are already gone from
    # PG, so the points are unreachable via retrieval joins on payload —
    # but they'd still be directly searchable, hence the loud warning).
    if stale_passage_rows:
        try:
            from qdrant_client import AsyncQdrantClient  # noqa: PLC0415
            from qdrant_client import models as qmodels  # noqa: PLC0415

            from app.services.qdrant_conn import qdrant_client_kwargs  # noqa: PLC0415

            stale_point_ids = [r["passage_id"] for r in stale_passage_rows]
            _qc = AsyncQdrantClient(**qdrant_client_kwargs())
            try:
                await _qc.delete(
                    collection_name="georag_chunks",
                    points_selector=qmodels.PointIdsList(points=stale_point_ids),
                    wait=False,
                )
            finally:
                await _qc.close()
            log.info(
                "ingest_pdf.persist: deleted %d stale qdrant point(s) for "
                "report=%s", len(stale_point_ids), report_id,
            )
        except Exception as gc_exc:  # noqa: BLE001
            log.warning(
                "ingest_pdf.persist: stale-passage qdrant delete failed for "
                "report=%s (%d points): %s — PG rows are gone; the orphaned "
                "points remain retrievable until a payload audit or "
                "re-bootstrap removes them.",
                report_id, len(stale_passage_rows), gc_exc,
            )

    log.info(
        "ingest_pdf.persist done report_id=%s parser=%s sections=%d tables=%d total=%dms",
        report_id, final.parser_used,
        final.sections_count, final.resource_tables_count,
        final.parse_duration_ms + final.persist_duration_ms,
    )

    # F2 (2026-08-11) — stamp this run's report_id onto its ingest_progress
    # row now (not at completion) so the embed completion sweep and
    # stale_run_detector can test "fully embedded?" against THIS document
    # only. Embeds serialize per workspace (embed wf max_runs=1), so on bulk
    # imports the old project-wide predicate timed out runs whose own
    # document had long finished embedding.
    #
    # Both writes (and the diagnostics below) already happened INSIDE persist's
    # transaction; this is only the fallback for when that savepoint write
    # failed, so the row is never left without them.
    if input.workspace_id and not _progress_stamped:
        await ingest_progress.mark_report_id(
            workspace_id=str(input.workspace_id),
            minio_key=input.minio_key,
            report_id=report_id,
        )
        await ingest_progress.mark_run_diagnostics(
            workspace_id=str(input.workspace_id),
            minio_key=input.minio_key,
            rows_written=final.passages_written,
            warnings=final.run_warnings,
        )
    if input.workspace_id:
        # The verdict is on the row for the closers that never see this
        # task's output (the embed completion sweep, stale_run_detector's race
        # recovery). rows_written is the narrative passages written, so a
        # document that produced no text is `partial`, not `completed`.
        _blocking = [w for w in final.run_warnings if w.get("severity") != "info"]
        if _blocking or final.passages_written == 0:
            log.warning(
                "ingest_pdf.persist: run for key=%s will finish PARTIAL "
                "(passages_written=%d): %s",
                input.minio_key, final.passages_written,
                "; ".join(str(w.get("code")) for w in _blocking) or "no passages",
            )

    # Trigger embedding for this project so chunks land in qdrant
    # immediately, instead of waiting for the 20:45 UTC daily cron.
    # Fire-and-forget — embedding can take minutes for big PDFs; we don't
    # block persist on it. The workflow's own retries/idempotency handle
    # transient failures.
    #
    # Passing the typed input model (not a dict) so pydantic validation
    # succeeds on the worker side. Earlier dict-form raised a
    # PydanticSerializationUnexpectedValue warning and the wrapped run
    # silently no-op'd.
    if input.project_id:
        try:
            from app.hatchet_workflows.embed_pending_passages import (
                EmbedPendingPassagesInput,
                embed_pending_passages_wf,
            )
            embed_input = EmbedPendingPassagesInput(
                workspace_id=str(input.workspace_id) if input.workspace_id else workspace_id_str,
                project_id=str(input.project_id),
                batch_size=32,
            )
            await embed_pending_passages_wf.aio_run_no_wait(embed_input)
            log.info("ingest_pdf.persist: embed_pending_passages dispatched for project=%s", input.project_id)
        except Exception as embed_err:
            log.warning(
                "ingest_pdf.persist: failed to dispatch embed workflow: %s — "
                "chunks will be picked up by daily cron at 20:45 UTC",
                embed_err,
            )

    return final


# ---- Step 4: embed-verify ----------------------------------------------------
# F3 (2026-08-11) — keep in lockstep with the eligibility predicate in
# app/services/ingest/passage_embedder.py (embed_pending_passages SELECT):
# passages whose OCR text is known-bad ('rejected' / 'pending_reocr') are
# never embedded, so counting them as "unembedded" here would wedge the run
# at embed_verify forever.
# Page-image passages (modality = 'image') are excluded as well: they are
# best-effort coverage (the Embed 5 image request shape is unverified), and one
# whose embed fails must not hold a run with good text open until the stale
# sweep times it out. Their absence is reported as an INFO warning
# (``image_passages_unembedded``), not hidden.
_EMBEDDABLE_OCR_PREDICATE = (
    "((p.ocr_status IS NULL OR p.ocr_status NOT IN ('rejected', 'pending_reocr')) "
    "AND p.modality IS DISTINCT FROM 'image')"
)


@contextlib.asynccontextmanager
async def _scoped_acquire(pool: Any, workspace_id: str, site: str):
    """Acquire a connection with the workspace bound for the read it is about to do.

    silver.document_passages is FAIL-CLOSED (tenant_isolation, no GUC -> no rows)
    and the worker connects as georag_app (NOBYPASSRLS). embed_verify counted
    unembedded passages on a bare connection, so in production the count was
    always 0: every run was closed "Ingestion complete; all chunks embedded"
    the moment persist finished, before a single vector existed, and the
    backstop re-dispatch below could never fire. Same defect, and same fix, as
    stale_run_detector's HAT-1 check. Bound per acquire, never per pool:
    asyncpg resets session state when a connection is released.
    """
    async with pool.acquire() as conn, conn.transaction():
        await bind_workspace_scope(conn, workspace_id=workspace_id, site=site)
        yield conn


# Safety net for the BattleNorth-style race where the inline embed dispatch
# from persist gets lost between Hatchet retries. Quickly polls the project's
# unembedded passage count and re-dispatches the embed workflow if anything
# is still pending. This is belt-and-suspenders alongside the every-10-min
# cron — gives users near-realtime "I just uploaded this and chat sees it".
@ingest_pdf.task(
    execution_timeout="60s", schedule_timeout="2h", retries=1, parents=[persist],
    backoff_factor=8.0, backoff_max_seconds=60,
)
async def embed_verify(input: IngestPdfInput, ctx: Context) -> dict:
    """Phase 8 (2026-05-22) — single check + dispatch, no polling loop.

    Previously this task polled the unembedded passage count every 15 s
    for up to 90 s before redispatching. Under heavy concurrent load
    (Phase 5 enabled 4× parallel parses → embed queue depth can exceed
    90 s), the polling caused noisy retries on every parse and burned
    PG round-trips for no benefit — the inline embed dispatched from
    `persist` is the primary path; this task is just a backstop.

    The simplification:
      1. One SELECT to count unembedded passages for this project.
      2. If zero, exit (inline already finished — rare but possible).
      3. Otherwise dispatch `embed_pending_passages_wf` and exit.
         Safe because that workflow is idempotent: it SELECTs
         `embedding_id IS NULL` and qdrant upserts on `point_id`.
         Concurrent runs race on the same rows; the loser does a few
         duplicate encodes but produces identical points.

    Worst-case wasted work is ~20-40 s of duplicate encodes per batch,
    which is less than the polling overhead it replaces. The 10-min
    `*/10 * * * *` cron remains the durability backstop.
    """
    if not input.project_id:
        return {"ok": True, "skipped": True, "reason": "no project_id"}

    # 2026-08-14 — preflight-rejected runs were marked FAILED by persist
    # (no report row written). Without this guard the zero-unembedded
    # check below would try to complete the run and broadcast "completed"
    # via the (ws, key) fallback re-query even though the row is failed.
    try:
        _parsed_out = ctx.task_output(parse)
        _parsed_out = (
            _parsed_out.model_dump()
            if hasattr(_parsed_out, "model_dump") else dict(_parsed_out)
        )
        if (_parsed_out.get("parser_used") or "") == "skipped":
            return {"ok": False, "skipped": True, "reason": "preflight_rejected"}
    except Exception:  # noqa: BLE001 — defensive; fall through to normal path
        pass

    if input.project_id and input.workspace_id:
        await ingest_progress.mark_started(
            workspace_id=str(input.workspace_id),
            project_id=str(input.project_id),
            minio_key=input.minio_key,
            step="embed_verify",
            run_id=input.run_id,
        )

    _verify_workspace = str(input.workspace_id) if input.workspace_id else LEGACY_DEFAULT_TENANT_UUID
    pool = await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=1, statement_cache_size=0,
    )
    try:
        async with _scoped_acquire(pool, _verify_workspace, "hatchet.ingest_pdf.embed_verify") as conn:
            row = await conn.fetchrow(
                f"""
                SELECT count(*) AS unembedded
                FROM silver.document_passages p
                JOIN silver.reports r ON r.report_id = p.document_id
                WHERE r.project_id = $1::uuid
                  AND p.embedding_id IS NULL
                  AND {_EMBEDDABLE_OCR_PREDICATE}
                """,
                str(input.project_id),
            )
            unembedded = int(row["unembedded"] or 0)

        if unembedded == 0:
            if input.workspace_id:
                # Close the run WITH what persist found. This used to call
                # the bare legacy mark_completed(), which passes neither
                # rows_written nor warnings, so terminal_status() could only
                # answer 'completed' -- and a 400-page scan whose OCR failed
                # (zero passages, nothing to embed, hence unembedded == 0 on
                # the very first check) broadcast "all chunks embedded".
                _run_warnings: list[dict] | None = None
                _rows_written: int | None = None
                _persisted_report_id: str | None = None
                try:
                    _persisted = ctx.task_output(persist)
                    _persisted = (
                        _persisted.model_dump()
                        if hasattr(_persisted, "model_dump") else dict(_persisted)
                    )
                    _rows_written = int(_persisted.get("passages_written", 0) or 0)
                    _run_warnings = list(_persisted.get("run_warnings") or [])
                    _persisted_report_id = _persisted.get("report_id")
                except Exception as exc:  # noqa: BLE001 -- fall back to the row
                    log.warning(
                        "embed_verify: could not read persist output key=%s "
                        "(%s) -- using the diagnostics stored on the run row",
                        input.minio_key, exc,
                    )
                # Page-image passages are best-effort and do not gate this
                # close (see _EMBEDDABLE_OCR_PREDICATE); say so when some
                # never embedded, as an INFO warning (does not turn it amber).
                if _persisted_report_id and _run_warnings is not None:
                    try:
                        async with _scoped_acquire(
                            pool, _verify_workspace,
                            "hatchet.ingest_pdf.embed_verify.images",
                        ) as _img_conn:
                            _img_row = await _img_conn.fetchrow(
                                "SELECT count(*) AS n "
                                "FROM silver.document_passages p "
                                "WHERE p.document_id = $1::uuid "
                                "  AND p.modality = 'image' "
                                "  AND p.embedding_id IS NULL",
                                str(_persisted_report_id),
                            )
                        _img_unembedded = int(_img_row["n"] or 0) if _img_row else 0
                        if _img_unembedded:
                            _run_warnings.append(
                                ingest_progress.image_passages_unembedded_warning(
                                    _img_unembedded
                                )
                            )
                    except Exception as exc:  # noqa: BLE001 -- diagnostics only
                        log.warning(
                            "embed_verify: unembedded page-image count failed "
                            "key=%s (%s)", input.minio_key, exc,
                        )
                try:
                    run_id = await ingest_progress.lookup_active_run_id(
                        workspace_id=str(input.workspace_id),
                        minio_key=input.minio_key,
                    )
                    transitioned = False
                    if run_id is not None:
                        transitioned = bool(
                            await ingest_progress.mark_completed_by_run(
                                run_id=run_id,
                                rows_written=_rows_written,
                                warnings=_run_warnings,
                            )
                        )
                    else:
                        log.warning(
                            "embed_verify: no active run for (ws=%s, key=%s) "
                            "-- already closed by a sweep, nothing to broadcast",
                            input.workspace_id, input.minio_key,
                        )
                    # Reliability spec -- broadcast terminal completion event so
                    # the IngestionRuns UI can flip immediately instead of
                    # waiting for its next poll. Only for the writer that
                    # actually transitioned the row (a sweep that got there
                    # first has already broadcast), and with the status the row
                    # EARNED -- 'partial' carries the first warning as its
                    # message instead of "all chunks embedded".
                    if transitioned and run_id and input.project_id:
                        from app.services.laravel_bridge import post_ingestion_progress

                        _status, _message = await ingest_progress.terminal_outcome(
                            run_id=run_id,
                            default_message="Ingestion complete; all chunks embedded.",
                        )
                        await post_ingestion_progress(
                            workspace_id=str(input.workspace_id),
                            project_id=str(input.project_id),
                            run_id=run_id,
                            stage="embedding",
                            status=_status,
                            message=_message,
                        )
                except Exception as exc:
                    log.warning(
                        "embed_verify: completion broadcast failed key=%s err=%s",
                        input.minio_key, exc,
                    )
            return {"ok": True, "unembedded_final": 0}

        # Unembedded passages remain — advance the progress to the final
        # 'embedding' step so the UI shows the bar is in the home stretch.
        if input.workspace_id:
            await ingest_progress.mark_started(
                workspace_id=str(input.workspace_id),
                project_id=str(input.project_id),
                minio_key=input.minio_key,
                step="embedding",
                run_id=input.run_id,
            )

        # Unembedded passages remain — dispatch embed_pending_passages.
        try:
            from app.hatchet_workflows.embed_pending_passages import (
                EmbedPendingPassagesInput,
                embed_pending_passages_wf,
            )
            if input.workspace_id:
                wsid = str(input.workspace_id)
            else:
                wsid = LEGACY_DEFAULT_TENANT_UUID
                with contextlib.suppress(Exception):
                    WORKSPACE_RESOLUTION_FAILURES.labels(
                        site="ingest_pdf.dispatch_embed"
                    ).inc()
            embed_input = EmbedPendingPassagesInput(
                workspace_id=wsid,
                project_id=str(input.project_id),
                batch_size=64,
            )
            await embed_pending_passages_wf.aio_run_no_wait(embed_input)
            log.info(
                "ingest_pdf.embed_verify: dispatched embed for project=%s "
                "(unembedded_observed=%d)",
                input.project_id, unembedded,
            )
            return {
                "ok": True,
                "redispatched": True,
                "unembedded_observed": unembedded,
            }
        except Exception as exc:
            log.warning(
                "ingest_pdf.embed_verify: dispatch failed: %s — "
                "10-min cron will pick up", exc,
            )
            return {
                "ok": False,
                "error": str(exc),
                "unembedded_observed": unembedded,
            }
    finally:
        await pool.close()




# =============================================================================
# Workflow-level failure hook — reliability spec Fix 1c.
# =============================================================================
# Runs whenever the workflow reaches a failed terminal state (task retries
# exhausted, worker crash with clean signal, explicit cancellation). Its job:
#
#   1. Resolve the run_id from (workspace_id, minio_key) — preflight may not
#      have written it yet if the failure happened during input validation.
#   2. Conditional-update silver.ingest_progress.status to 'failed' (or
#      'cancelled' if the task event explicitly says so).
#   3. POST the terminal event to Laravel for Reverb broadcast.
#   4. Do NOT call mv_refresh_silver.
#   5. Do NOT bump data_version in Redis.
#
# The conditional update ensures we don't overwrite a state that mark_failed
# inside the task body already wrote, and we don't double-fire broadcasts if
# Hatchet retries the on_failure task itself (retries=2).
@ingest_pdf.on_failure_task(
    name="on_failure",
    execution_timeout="30s",
    schedule_timeout="30m",
    retries=2,
)
async def on_failure(input: IngestPdfInput, ctx: Context) -> dict:
    """Workflow-level failure hook.

    Fires from every failure path that can leave the run in 'started':
      - All retries exhausted on any task
      - Worker crash with a clean SIGTERM (SIGKILL is caught by stale_run_sweep)
      - Explicit workflow cancellation (concurrency-queue expiry,
        manual cancel via the Hatchet UI)
    """
    from app.hatchet_workflows import _progress as ingest_progress
    from app.services.laravel_bridge import post_ingestion_progress

    workspace_id = str(input.workspace_id)
    project_id = str(input.project_id) if input.project_id else None
    minio_key = input.minio_key

    # Resolve the active run for this file. start_run is called by the
    # preflight task, so a run_id should exist unless we failed in input
    # validation before preflight ever ran. In that edge case there's
    # nothing to update.
    run_id = await ingest_progress.lookup_active_run_id(
        workspace_id=workspace_id, minio_key=minio_key,
    )
    if run_id is None:
        log.warning(
            "ingest_pdf.on_failure: no active run found for (ws=%s, key=%s) — "
            "skipping terminal update", workspace_id, minio_key,
        )
        return {"updated": False, "reason": "no_active_run"}

    # Fetch current_stage so the IngestionRuns UI can show "failed at parse"
    # instead of just "failed". get_run is a single SELECT against the pool.
    row = await ingest_progress.get_run(run_id=run_id)
    current_stage = (row or {}).get("current_stage") or "unknown"

    # 2026-08-16 — capture the REAL upstream exception instead of a
    # hardcoded placeholder. `ctx.task_run_errors` is Hatchet's own
    # per-task error map, populated specifically for use inside an
    # on_failure hook (engine >= v0.53.10; we run v0.89.7). Before this
    # fix every failure — worker-restart interruption, a genuine bug, a
    # cancellation — recorded the exact same uninformative fallback string
    # in silver.ingest_progress.error_text, making root-causing recurring
    # persist-stage failures from the IngestionRuns UI alone impossible.
    try:
        task_errors = ctx.task_run_errors
    except Exception as exc:  # noqa: BLE001 — never let diagnostics block the hook
        log.warning("ingest_pdf.on_failure: could not read task_run_errors: %s", exc)
        task_errors = {}
    if task_errors:
        error_detail = "; ".join(f"{name}: {msg}" for name, msg in task_errors.items())
    else:
        error_detail = "no task_run_errors available (worker crash/cancellation with no captured exception)"

    transitioned = await ingest_progress.mark_failed_by_run(
        run_id=run_id,
        stage=current_stage,
        error=error_detail,
    )

    if transitioned and project_id:
        try:
            await post_ingestion_progress(
                workspace_id=workspace_id,
                project_id=project_id,
                run_id=run_id,
                stage=current_stage,
                status="failed",
                message="Workflow exhausted retries or was cancelled.",
            )
        except Exception as exc:
            log.warning("ingest_pdf.on_failure: broadcast failed run=%s: %s", run_id, exc)

    return {
        "updated": transitioned,
        "run_id": run_id,
        "current_stage": current_stage,
    }


__all__ = ["ingest_pdf", "IngestPdfInput", "ParseOut", "IngestPdfFinalOut"]
