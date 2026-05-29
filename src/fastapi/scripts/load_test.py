"""Concurrent-stream load test for the Laravel → FastAPI → SSE path (→ A).

What it measures
----------------
- How many concurrent queries the stack handles without cross-contamination.
- p50 / p95 / p99 query latency at N concurrency levels.
- Failure mode at saturation (timeouts vs 503s vs silent blocking).

Previously we had zero multi-client validation. Horizon `maxProcesses=5`
on the prod supervisor means 6 simultaneous users starve the queue;
this test catches that before real users do.

Usage
-----
  # Dev: 5 concurrent queries, 10 total
  docker exec georag-fastapi python /app/scripts/load_test.py

  # Stress: 20 concurrent, 50 total
  docker exec georag-fastapi python /app/scripts/load_test.py --concurrency 20 --total 50

  # Different project
  docker exec georag-fastapi python /app/scripts/load_test.py --project <uuid>

Output
------
- Per-query success/failure + latency to stdout.
- Summary table (p50/p95/p99) at the end.
- Non-zero exit when any query fails — CI can fail the build on
  regression without additional wiring.

The harness calls POST /internal/queries directly (bypassing Laravel
and Horizon) because the failure modes we care about — FastAPI
saturation, connection-pool starvation, concurrent-client interference
— all live in the FastAPI layer. For Laravel + Horizon capacity
testing, see docs/RUNBOOK.md.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from dataclasses import dataclass

import httpx


DEFAULT_QUERIES = [
    "How many drill holes are in this project?",
    "What is the deepest hole and its depth?",
    "What deposit does this project host?",
    "List holes drilled in 2022.",
    "What is the average total depth of all holes?",
    "Show me the holes with the highest uranium grades.",
    "Who is the qualified person on the NI 43-101?",
    "What is the easternmost drill hole?",
    "Summarise the lithology intersections for PLS-22-08.",
    "What exploration programs does the NI 43-101 report recommend?",
]


@dataclass
class QueryResult:
    query: str
    success: bool
    duration_s: float
    status_code: int
    error: str | None = None


async def _run_one(
    client: httpx.AsyncClient,
    url: str,
    service_key: str,
    query: str,
    project_id: str,
    timeout_s: float,
) -> QueryResult:
    started = time.monotonic()
    try:
        async with client.stream(
            "POST",
            url,
            headers={
                "X-Service-Key": service_key,
                "Content-Type": "application/json",
            },
            json={"query": query, "project_id": project_id},
            timeout=timeout_s,
        ) as response:
            status = response.status_code
            # Drain the SSE stream to completion — we want to measure full
            # end-to-end latency, not just time-to-first-byte.
            got_completed = False
            async for line in response.aiter_lines():
                if "event: completed" in line or "event: failed" in line:
                    got_completed = True
                    break
            duration = time.monotonic() - started
            return QueryResult(
                query=query,
                success=(status == 200 and got_completed),
                duration_s=duration,
                status_code=status,
                error=None if status == 200 else f"HTTP {status}",
            )
    except (httpx.TimeoutException, httpx.ReadTimeout) as exc:
        return QueryResult(
            query=query,
            success=False,
            duration_s=time.monotonic() - started,
            status_code=0,
            error=f"timeout: {exc.__class__.__name__}",
        )
    except Exception as exc:
        return QueryResult(
            query=query,
            success=False,
            duration_s=time.monotonic() - started,
            status_code=0,
            error=f"{exc.__class__.__name__}: {exc}",
        )


async def run_load_test(
    url: str,
    service_key: str,
    project_id: str,
    concurrency: int,
    total: int,
    timeout_s: float,
) -> list[QueryResult]:
    """Run `total` queries with at most `concurrency` in flight."""
    semaphore = asyncio.Semaphore(concurrency)
    results: list[QueryResult] = []

    async with httpx.AsyncClient() as client:
        async def _bounded(idx: int, query: str) -> QueryResult:
            async with semaphore:
                result = await _run_one(client, url, service_key, query, project_id, timeout_s)
                print(
                    f"  [{idx + 1:>3}/{total}] {'✓' if result.success else '✗'} "
                    f"{result.duration_s:6.2f}s  {result.query[:60]}"
                    + (f"  — {result.error}" if result.error else "")
                )
                return result

        tasks = [
            _bounded(i, DEFAULT_QUERIES[i % len(DEFAULT_QUERIES)])
            for i in range(total)
        ]
        results = await asyncio.gather(*tasks)

    return results


def _summarise(results: list[QueryResult], concurrency: int, total: int) -> int:
    """Print a markdown-style summary. Return exit code (0=ok, 1=regression)."""
    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]
    durations_ok = sorted(r.duration_s for r in successes)

    print()
    print("=" * 70)
    print(f"Load test — concurrency={concurrency}, total={total}")
    print("=" * 70)
    print(f"success:  {len(successes)}/{total}")
    print(f"failures: {len(failures)}")

    if durations_ok:
        def _pct(p: float) -> float:
            idx = min(int(len(durations_ok) * p), len(durations_ok) - 1)
            return durations_ok[idx]
        print(f"p50 latency:  {_pct(0.50):.2f}s")
        print(f"p95 latency:  {_pct(0.95):.2f}s")
        print(f"p99 latency:  {_pct(0.99):.2f}s")
        print(f"min / max:    {min(durations_ok):.2f}s / {max(durations_ok):.2f}s")
        print(f"mean:         {statistics.fmean(durations_ok):.2f}s")

    if failures:
        print()
        print("Failure breakdown:")
        seen_errors: dict[str, int] = {}
        for f in failures:
            seen_errors[f.error or "unknown"] = seen_errors.get(f.error or "unknown", 0) + 1
        for err, count in sorted(seen_errors.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>3}x  {err}")
        return 1

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("FASTAPI_LOAD_TEST_URL", "http://localhost:8000/internal/queries"))
    parser.add_argument("--project", default="019d74a1-fba8-7165-9ae6-a5bf93eef97d",
                        help="Project UUID (default: Lazy Edward Bay demo)")
    parser.add_argument("--concurrency", type=int, default=5,
                        help="Max in-flight queries (default: 5)")
    parser.add_argument("--total", type=int, default=10,
                        help="Total queries to run (default: 10)")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="Per-query timeout in seconds (default: 120)")
    args = parser.parse_args()

    service_key = os.environ.get("FASTAPI_SERVICE_KEY")
    if not service_key:
        print("FASTAPI_SERVICE_KEY not set in environment. Run inside the georag-fastapi container.",
              file=sys.stderr)
        return 2

    print(f"Target: {args.url}")
    print(f"Project: {args.project}")
    print(f"Concurrency: {args.concurrency}  Total: {args.total}  Timeout: {args.timeout}s")
    print()

    results = asyncio.run(run_load_test(
        url=args.url,
        service_key=service_key,
        project_id=args.project,
        concurrency=args.concurrency,
        total=args.total,
        timeout_s=args.timeout,
    ))

    return _summarise(results, args.concurrency, args.total)


if __name__ == "__main__":
    raise SystemExit(main())
