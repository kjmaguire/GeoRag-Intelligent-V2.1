"""Phase A — inspect a large zip archive without full extraction.

Doc-phase 173. Designed for the 200GB TIF-bundle case: zips-within-zips
where every leaf file is a TIFF scan of a historical exploration record.

Pipeline:
  1. Open the outer zip's central directory (no decompression)
  2. For each inner zip entry, open it as a stream and walk its TOC
  3. For each TIFF entry, slice off the first ~64KB and parse the IFD
     header to extract width/height/pages/compression/dpi
  4. Cluster by inner-zip directory name (`guessed_project`)
  5. Write rows to bronze.ingest_manifest + update bronze.ingest_runs
     progress fields

Critical design choices:
  - Streaming: never extracts the full 200GB. Only reads enough bytes
    per file to parse the TIFF IFD (typically <16KB)
  - Idempotent re-runs: rows are tagged by `run_id` so a partial run
    can be resumed with `--resume <run_id>` (TODO follow-on)
  - No pixel decode: Pillow's `Image.open` lazy-loads metadata
    without decoding pixel data
  - Progress checkpoints every N files: bronze.ingest_runs.files_seen
    increments live so /admin/ingestion-review can watch progress

Usage:
  python scripts/inspect_ingest_zip.py /path/to/outer.zip
  python scripts/inspect_ingest_zip.py /path/to/outer.zip --mode outer-toc-only
  python scripts/inspect_ingest_zip.py /path/to/outer.zip --max-files 10000  # smoke test
  python scripts/inspect_ingest_zip.py /path/to/outer.zip --progress-every-seconds 15

Modes (doc-phase 178 refactor):
  full           — walk every TIF in every inner zip (slow for large archives)
  outer-toc-only — list only outer-zip entries; record each inner zip as one
                   manifest row with type='inner_zip'. FAST first-inventory
                   pass — typically seconds even for 200GB archives.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import logging
import os
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

import asyncpg

# Pillow is the standard TIFF reader — lazy metadata, no full decode
try:
    from PIL import Image, TiffImagePlugin
    Image.MAX_IMAGE_PIXELS = None  # disable decompression-bomb limit for headers-only
except ImportError as e:
    print(f"FATAL: Pillow not installed ({e}). Add it to requirements.", file=sys.stderr)
    sys.exit(2)


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("ingest.phase_a")


# TIFF magic bytes — recognized at offset 0 of every TIFF file.
_TIFF_MAGIC = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")


@dataclass
class TiffMetadata:
    """Header-only metadata extracted from a TIFF file."""

    width: int | None = None
    height: int | None = None
    pages: int = 1
    compression: str | None = None
    bits_per_pixel: int | None = None
    dpi_x: int | None = None
    dpi_y: int | None = None
    anomalies: list[str] = field(default_factory=list)


def _detect_file_type(name: str, head_bytes: bytes) -> str:
    """Quick file-type classifier — name + first bytes."""
    lower = name.lower()
    if any(head_bytes.startswith(m) for m in _TIFF_MAGIC):
        return "tiff"
    if lower.endswith((".tif", ".tiff")):
        return "tiff"  # accept extension-only when bytes weren't readable
    if lower.endswith(".pdf"):
        return "pdf"
    if lower.endswith(".zip"):
        return "zip"
    if lower.endswith((".jpg", ".jpeg")):
        return "jpeg"
    if lower.endswith((".png",)):
        return "png"
    if lower.endswith((".las",)):
        return "las"
    if lower.endswith((".csv",)):
        return "csv"
    if lower.endswith((".xls", ".xlsx")):
        return "xlsx"
    if lower.endswith((".shp",)):
        return "shapefile"
    return "unknown"


def _extract_tiff_metadata(blob: bytes, file_path: str) -> TiffMetadata:
    """Parse TIFF IFD via Pillow without decoding pixels."""
    md = TiffMetadata()
    try:
        with Image.open(io.BytesIO(blob)) as img:
            md.width = int(img.width)
            md.height = int(img.height)
            md.bits_per_pixel = int(sum(img.getbands().__len__() and (img.mode.count('1') and 1 or 8) for _ in [None]))  # rough
            # Better: read from tag — TIFFs use tag 258 (BitsPerSample)
            tag_v2 = getattr(img, "tag_v2", {}) or {}
            bps = tag_v2.get(258)
            if bps:
                if isinstance(bps, tuple):
                    md.bits_per_pixel = sum(int(x) for x in bps)
                else:
                    md.bits_per_pixel = int(bps)
            comp = tag_v2.get(259)
            if comp is not None:
                # Pillow exposes Compression enum strings via TiffImagePlugin
                comp_map = TiffImagePlugin.COMPRESSION_INFO if hasattr(TiffImagePlugin, "COMPRESSION_INFO") else {}
                md.compression = comp_map.get(int(comp), f"code_{int(comp)}")
            dpi = img.info.get("dpi")
            if dpi and isinstance(dpi, tuple) and len(dpi) >= 2:
                md.dpi_x = int(dpi[0]) if dpi[0] else None
                md.dpi_y = int(dpi[1]) if dpi[1] else None
            # Multi-page TIFF — count frames lazily
            try:
                pages = 1
                while True:
                    try:
                        img.seek(pages)
                        pages += 1
                        if pages > 10000:
                            md.anomalies.append("page_count_overflow")
                            break
                    except EOFError:
                        break
                md.pages = pages
            except Exception as e:
                md.anomalies.append(f"page_count_error:{type(e).__name__}")
    except Exception as e:
        md.anomalies.append(f"header_parse_error:{type(e).__name__}:{str(e)[:80]}")
    return md


def _guess_project_from_path(outer_zip: str, inner_zip: str | None, file_path: str) -> str:
    """First-pass clustering heuristic.

    Priority order:
      1. Inner-zip directory name (usually the archival box / project ID)
      2. First path component within the inner zip
      3. Outer zip basename (single-project archives)
    """
    if inner_zip:
        return PurePosixPath(inner_zip).stem
    parts = PurePosixPath(file_path).parts
    if len(parts) > 1:
        return parts[0]
    return PurePosixPath(outer_zip).stem


def _cluster_key(inner_zip: str | None, guessed_project: str) -> str:
    """Cluster key — used by /admin/ingestion-review for grouping."""
    if inner_zip:
        return f"innerzip::{inner_zip}"
    return f"project::{guessed_project}"


def _build_dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag_app")
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


async def _create_run(conn: asyncpg.Connection, source_path: str) -> UUID:
    size = None
    try:
        size = os.path.getsize(source_path)
    except OSError:
        pass
    return await conn.fetchval(
        """
        INSERT INTO bronze.ingest_runs (source_path, source_size_bytes, status)
        VALUES ($1, $2, 'running')
        RETURNING run_id
        """,
        source_path, size,
    )


async def _finalize_run(
    conn: asyncpg.Connection, run_id: UUID, status: str,
    error_text: str | None, summary: dict,
) -> None:
    await conn.execute(
        """
        UPDATE bronze.ingest_runs
           SET completed_at = NOW(),
               status = $2,
               error_text = $3,
               summary_payload = $4::jsonb
         WHERE run_id = $1
        """,
        run_id, status, error_text, _json_dump(summary),
    )


def _json_dump(obj: Any) -> str:
    import json
    return json.dumps(obj, default=str)


async def _bump_progress(
    conn: asyncpg.Connection, run_id: UUID, *,
    files_delta: int = 0, indexed_delta: int = 0, skipped_delta: int = 0,
    bytes_delta: int = 0,
) -> None:
    await conn.execute(
        """
        UPDATE bronze.ingest_runs
           SET files_seen = files_seen + $2,
               files_indexed = files_indexed + $3,
               files_skipped = files_skipped + $4,
               bytes_seen = bytes_seen + $5
         WHERE run_id = $1
        """,
        run_id, files_delta, indexed_delta, skipped_delta, bytes_delta,
    )


async def _insert_manifest_batch(
    conn: asyncpg.Connection, rows: list[dict],
) -> None:
    """Insert a batch of manifest rows.

    Every row's ``workspace_id`` is populated from the per-run context
    (set by ``run_phase_a``'s ``workspace_id`` parameter). The column is
    nullable at the schema level so legacy rows survive — but every
    NEW insert MUST carry workspace_id so the RLS IS-NULL exemption
    can eventually be dropped (see migration
    2026_05_25_170825_enable_rls_on_bronze_tenancy_tables).
    """
    if not rows:
        return
    await conn.executemany(
        """
        INSERT INTO bronze.ingest_manifest (
            workspace_id,
            run_id, outer_zip_path, inner_zip_path, file_path_in_zip, file_name,
            file_size_bytes, file_type, file_extension,
            tiff_width, tiff_height, tiff_pages, tiff_compression,
            tiff_bits_per_pixel, tiff_dpi_x, tiff_dpi_y,
            guessed_project, cluster_key, anomalies
        ) VALUES (
            $1::uuid,
            $2, $3, $4, $5, $6,
            $7, $8, $9,
            $10, $11, $12, $13,
            $14, $15, $16,
            $17, $18, $19::jsonb
        )
        """,
        [
            (
                r["workspace_id"],
                r["run_id"], r["outer_zip_path"], r["inner_zip_path"],
                r["file_path_in_zip"], r["file_name"],
                r["file_size_bytes"], r["file_type"], r["file_extension"],
                r["tiff_width"], r["tiff_height"], r["tiff_pages"],
                r["tiff_compression"], r["tiff_bits_per_pixel"],
                r["tiff_dpi_x"], r["tiff_dpi_y"],
                r["guessed_project"], r["cluster_key"],
                _json_dump(r["anomalies"]),
            )
            for r in rows
        ],
    )


# How many bytes to slice off the head of each file for TIFF header parse.
# A standard TIFF IFD lives well within the first 64KB; multi-page TIFFs
# may have IFDs further in, but Pillow's seek() reads the chained IFD
# pointers progressively so we'd need the full file. For Phase A we
# accept "page count for multi-page TIFFs may be undercounted" as a
# known trade-off; Phase B re-OCRs with full file access.
_HEAD_SLICE = 64 * 1024


def _walk_archive(
    outer_path: str,
    *,
    max_files: int | None = None,
    inner_filter: str | None = None,
    mode: str = "full",
):
    """Generator yielding (outer_zip, inner_zip_path, file_path, file_size, head_bytes).

    Args:
        outer_path: path to the outer zip archive
        max_files: stop after this many entries (smoke testing)
        inner_filter: substring filter on inner-zip names
        mode: 'full' (walk every TIF in every inner zip) or 'outer-toc-only'
              (just enumerate the outer zip's central directory — fast)
    """
    with zipfile.ZipFile(outer_path, "r") as outer:
        outer_entries = outer.infolist()
        log.info(
            "_walk_archive.outer_toc_loaded entries=%d mode=%s",
            len(outer_entries), mode,
        )
        yielded = 0
        for outer_entry in outer_entries:
            if outer_entry.is_dir():
                continue
            outer_name = outer_entry.filename
            outer_lower = outer_name.lower()
            if outer_lower.endswith(".zip"):
                # Inner-zip handling — fork on mode
                if inner_filter and inner_filter not in outer_name:
                    continue

                if mode == "outer-toc-only":
                    # Doc-phase 178 — fast inventory mode. Just record the
                    # existence + size of the inner zip; don't crack it.
                    # head_bytes is empty (no TIFF metadata for the inner zip
                    # itself).
                    yield (
                        outer_path, None, outer_name,
                        outer_entry.file_size, b"",
                    )
                    yielded += 1
                    if max_files and yielded >= max_files:
                        return
                    continue

                # mode == "full" — crack open the inner zip + walk its TOC
                try:
                    with outer.open(outer_entry, "r") as inner_stream:
                        # zipfile needs seek for central directory; load to memory
                        # only if reasonable size. For huge inner zips, streaming
                        # would need a seekable backing — TODO follow-on
                        inner_blob = inner_stream.read()
                    log.info(
                        "_walk_archive.inner_loaded name=%s size_mb=%.1f",
                        outer_name, len(inner_blob) / 1024 / 1024,
                    )
                    with zipfile.ZipFile(io.BytesIO(inner_blob), "r") as inner:
                        inner_entries = inner.infolist()
                        for inner_entry in inner_entries:
                            if inner_entry.is_dir():
                                continue
                            try:
                                with inner.open(inner_entry, "r") as f:
                                    head = f.read(_HEAD_SLICE)
                            except Exception as e:
                                head = b""
                                log.warning(
                                    "inner_file_open_failed inner=%s file=%s err=%s",
                                    outer_name, inner_entry.filename, e,
                                )
                            yield (
                                outer_path, outer_name, inner_entry.filename,
                                inner_entry.file_size, head,
                            )
                            yielded += 1
                            if max_files and yielded >= max_files:
                                return
                except (zipfile.BadZipFile, OSError) as e:
                    log.warning("inner_zip_corrupt name=%s err=%s", outer_name, e)
            else:
                # Top-level non-zip entry (loose file inside the outer zip)
                try:
                    with outer.open(outer_entry, "r") as f:
                        head = f.read(_HEAD_SLICE)
                except Exception as e:
                    head = b""
                    log.warning(
                        "outer_file_open_failed file=%s err=%s",
                        outer_name, e,
                    )
                yield (
                    outer_path, None, outer_name,
                    outer_entry.file_size, head,
                )
                yielded += 1
                if max_files and yielded >= max_files:
                    return


async def run_phase_a(
    outer_path: str,
    *,
    workspace_id: str,
    max_files: int | None = None,
    inner_filter: str | None = None,
    batch_size: int = 200,
    progress_every: int = 1000,
    progress_every_seconds: float = 30.0,
    mode: str = "full",
) -> dict:
    """Execute the Phase A walk + manifest write.

    Every manifest row is tagged with the supplied ``workspace_id`` so
    bronze.ingest_manifest's RLS policy can scope correctly (added
    2026-05-25 — see migration
    2026_05_25_170825_enable_rls_on_bronze_tenancy_tables).

    Returns the summary dict that gets persisted on bronze.ingest_runs.
    """
    conn = await asyncpg.connect(_build_dsn(), statement_cache_size=0)
    try:
        run_id = await _create_run(conn, outer_path)
        log.info("phase_a.started run_id=%s source=%s", run_id, outer_path)
        t0 = time.monotonic()

        batch: list[dict] = []
        clusters: dict[str, int] = {}
        type_counts: dict[str, int] = {}
        anomalies_count = 0
        files_seen = 0
        files_indexed = 0
        bytes_seen = 0

        last_progress_log = t0
        try:
            for outer_zip, inner_zip, file_path, file_size, head in _walk_archive(
                outer_path, max_files=max_files, inner_filter=inner_filter,
                mode=mode,
            ):
                files_seen += 1
                bytes_seen += file_size
                # Doc-phase 178 — outer-toc-only mode tags inner-zip entries
                # with file_type='inner_zip' so the manifest cleanly
                # distinguishes "inner archive we haven't cracked" from
                # "actual leaf TIF". Default detection still applies for
                # non-zip outer entries (loose files at outer level).
                if mode == "outer-toc-only" and file_path.lower().endswith(".zip"):
                    ftype = "inner_zip"
                else:
                    ftype = _detect_file_type(file_path, head)
                type_counts[ftype] = type_counts.get(ftype, 0) + 1

                row: dict[str, Any] = {
                    "workspace_id": workspace_id,
                    "run_id": run_id,
                    "outer_zip_path": outer_zip,
                    "inner_zip_path": inner_zip,
                    "file_path_in_zip": file_path,
                    "file_name": PurePosixPath(file_path).name,
                    "file_size_bytes": file_size,
                    "file_type": ftype,
                    "file_extension": (
                        PurePosixPath(file_path).suffix.lstrip(".").lower() or None
                    ),
                    "tiff_width": None,
                    "tiff_height": None,
                    "tiff_pages": None,
                    "tiff_compression": None,
                    "tiff_bits_per_pixel": None,
                    "tiff_dpi_x": None,
                    "tiff_dpi_y": None,
                    "anomalies": [],
                }

                if ftype == "tiff" and head:
                    md = _extract_tiff_metadata(head, file_path)
                    row["tiff_width"] = md.width
                    row["tiff_height"] = md.height
                    row["tiff_pages"] = md.pages
                    row["tiff_compression"] = md.compression
                    row["tiff_bits_per_pixel"] = md.bits_per_pixel
                    row["tiff_dpi_x"] = md.dpi_x
                    row["tiff_dpi_y"] = md.dpi_y
                    row["anomalies"] = md.anomalies
                    if md.anomalies:
                        anomalies_count += 1

                guessed = _guess_project_from_path(outer_zip, inner_zip, file_path)
                row["guessed_project"] = guessed
                row["cluster_key"] = _cluster_key(inner_zip, guessed)
                clusters[row["cluster_key"]] = clusters.get(row["cluster_key"], 0) + 1

                batch.append(row)
                files_indexed += 1

                if len(batch) >= batch_size:
                    await _insert_manifest_batch(conn, batch)
                    batch = []

                # Doc-phase 178 — dual progress checkpointing.
                # File-count based (every N files) catches fast-moving runs;
                # time-based (every N seconds) ensures we get a heartbeat
                # even when the script is stuck reading a single huge
                # inner-zip blob.
                now = time.monotonic()
                progress_due = (
                    files_seen % progress_every == 0 or
                    (now - last_progress_log) >= progress_every_seconds
                )
                if progress_due:
                    # Delta accounts for whatever increment landed since last
                    # checkpoint (rounded down to nearest checkpoint).
                    elapsed = now - t0
                    rate = files_seen / elapsed if elapsed else 0
                    await conn.execute(
                        """
                        UPDATE bronze.ingest_runs
                           SET files_seen = $2, files_indexed = $3
                         WHERE run_id = $1
                        """,
                        run_id, files_seen, files_indexed,
                    )
                    log.info(
                        "phase_a.progress files=%d clusters=%d types=%s rate=%.1f/s elapsed=%.1fs",
                        files_seen, len(clusters), dict(list(type_counts.items())[:5]),
                        rate, elapsed,
                    )
                    last_progress_log = now

            # Final batch
            if batch:
                await _insert_manifest_batch(conn, batch)

            # Final progress write — use absolute values so we don't double-count
            await conn.execute(
                """
                UPDATE bronze.ingest_runs
                   SET files_seen = $2, files_indexed = $3, bytes_seen = $4
                 WHERE run_id = $1
                """,
                run_id, files_seen, files_indexed, bytes_seen,
            )

            elapsed = time.monotonic() - t0
            summary = {
                "elapsed_seconds": round(elapsed, 2),
                "files_seen": files_seen,
                "files_indexed": files_indexed,
                "bytes_seen": bytes_seen,
                "type_counts": type_counts,
                "cluster_count": len(clusters),
                "top_clusters": sorted(
                    clusters.items(), key=lambda kv: -kv[1]
                )[:20],
                "anomalies_count": anomalies_count,
                "files_per_sec": round(files_seen / elapsed, 2) if elapsed else 0,
            }
            await _finalize_run(conn, run_id, "completed", None, summary)
            log.info(
                "phase_a.completed run_id=%s files=%d clusters=%d elapsed=%.1fs",
                run_id, files_seen, len(clusters), elapsed,
            )
            return summary
        except Exception as e:
            log.exception("phase_a.failed run_id=%s", run_id)
            await _finalize_run(
                conn, run_id, "failed", f"{type(e).__name__}: {e}", {
                    "files_seen": files_seen,
                    "files_indexed": files_indexed,
                    "type_counts": type_counts,
                },
            )
            raise
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="inspect_ingest_zip",
        description="Phase A — stream-walk a zip archive and write the ingest manifest.",
    )
    parser.add_argument("zip_path", help="Path to the outer zip archive")
    parser.add_argument(
        "--workspace-id", type=str, required=True,
        help="Workspace UUID this archive belongs to. Tags every "
             "bronze.ingest_manifest row so RLS scopes correctly.",
    )
    parser.add_argument(
        "--max-files", type=int, default=None,
        help="Stop after N files (smoke testing). Default: walk everything.",
    )
    parser.add_argument(
        "--inner-filter", type=str, default=None,
        help="Substring filter on inner-zip names. Only walk inner zips matching.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=200,
        help="Manifest insert batch size. Default 200.",
    )
    parser.add_argument(
        "--progress-every", type=int, default=1000,
        help="Log + persist progress every N files. Default 1000.",
    )
    parser.add_argument(
        "--progress-every-seconds", type=float, default=30.0,
        help="Also log + persist progress every N seconds even when no "
             "new files are processed (heartbeat for slow inner-zip reads). "
             "Default 30.",
    )
    parser.add_argument(
        "--mode", type=str, default="full",
        choices=["full", "outer-toc-only"],
        help="'full' walks every TIF in every inner zip (deep). "
             "'outer-toc-only' just enumerates the outer central "
             "directory (fast inventory). Default 'full'.",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.zip_path):
        print(f"FATAL: not a file: {args.zip_path}", file=sys.stderr)
        return 2

    # Validate workspace_id is a real UUID before we open the connection.
    try:
        UUID(args.workspace_id)
    except (ValueError, TypeError):
        print(f"FATAL: --workspace-id is not a valid UUID: {args.workspace_id!r}", file=sys.stderr)
        return 2

    summary = asyncio.run(run_phase_a(
        args.zip_path,
        workspace_id=args.workspace_id,
        max_files=args.max_files,
        inner_filter=args.inner_filter,
        batch_size=args.batch_size,
        progress_every=args.progress_every,
        progress_every_seconds=args.progress_every_seconds,
        mode=args.mode,
    ))
    print()
    print("=== Phase A summary ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
