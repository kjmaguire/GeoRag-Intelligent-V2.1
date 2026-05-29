"""Trigger the ingest_pdf workflow against the smoke PDF and wait for result."""
import asyncio
import os
import sys
import uuid

sys.path.insert(0, "/app")
from app.hatchet_workflows import hatchet
from app.hatchet_workflows.ingest_pdf import IngestPdfInput, ingest_pdf


async def main() -> int:
    workspace_id = uuid.UUID("00000000-acce-ed30-cccc-000000000030")
    correlation = f"phase1-smoke-{uuid.uuid4()}"
    payload = IngestPdfInput(
        workspace_id=workspace_id,
        project_id="phase1-smoke",
        minio_key="reports/phase1-smoke/PLS-2024-Technical-Report.pdf",
        file_size=17722,
        vendor_profile_id=None,
        correlation_token=correlation,
        actor_id=None,
    )

    print(f"triggering ingest_pdf with correlation={correlation}")
    ref = await ingest_pdf.aio_run_no_wait(payload)
    print(f"  workflow_run_id={ref.workflow_run_id}")
    print(f"  waiting for result...")

    try:
        result = await asyncio.wait_for(ref.aio_result(), timeout=300)
        print(f"  result keys: {sorted(result.keys()) if isinstance(result, dict) else type(result).__name__}")
        if isinstance(result, dict):
            persist = result.get("persist", result)
            print(f"  persist.sha256          = {persist.get('sha256', '')[:16]}…")
            print(f"  persist.parser_used     = {persist.get('parser_used')}")
            print(f"  persist.parse_quality_pct = {persist.get('parse_quality_pct')}")
            print(f"  persist.page_count      = {persist.get('page_count')}")
            print(f"  persist.sections_count  = {persist.get('sections_count')}")
            print(f"  persist.tables_count    = {persist.get('resource_tables_count')}")
            print(f"  persist.duration_ms     = {persist.get('duration_ms')}")
            print(f"  persist.shadow_runs_id  = {persist.get('shadow_runs_id')}")
            warnings = persist.get('warnings', []) or []
            if warnings:
                print(f"  persist.warnings ({len(warnings)}):")
                for w in warnings[:5]:
                    print(f"    - {w}")
        return 0
    except asyncio.TimeoutError:
        print("  TIMEOUT after 300s — check worker logs")
        return 1
    except Exception as e:
        print(f"  workflow raised: {type(e).__name__}: {e}")
        return 1


sys.exit(asyncio.run(main()))
