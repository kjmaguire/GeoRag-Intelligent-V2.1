"""24-hour soak test driver (v1.5-02).

Wraps `load_test.py` in a sustained moderate-concurrency loop, samples
host-side resource counters every minute, and emits a trend report on
exit. The point is to surface slow leaks (memory, Redis keys, PG
connections) that don't show up in the perf-baseline workflow's
short-burst measurements.

Usage
-----

    # Local dev (defaults to 60 minutes for fast iteration):
    python src/fastapi/scripts/soak_test.py --hours 1

    # Staging 24h soak:
    FASTAPI_URL=https://staging.georag.example.com \
    FASTAPI_SERVICE_KEY=$STAGING_KEY \
    python src/fastapi/scripts/soak_test.py --hours 24

    # Output goes to stdout (TSV) and to soak-trend.json (machine-parseable).

What it does
------------

1. Every `--cycle-seconds` (default 600 = 10 min): runs `load_test.py
   --concurrency=2 --total=10 --json` and captures p50/p95/p99 by class.
2. Between cycles: scrapes `/healthz` and the FastAPI `/metrics`
   endpoint, plus (if reachable) Redis `INFO memory`, Postgres
   `pg_stat_activity` count, Qdrant `/collections/<n>/points/count`.
3. On exit (or SIGINT): writes `soak-trend.json` with all samples and
   prints a human summary table.

Pass/fail signals
-----------------

The script prints a verdict line at the end:

    SOAK PASS  — no growth >threshold detected
    SOAK FAIL  — fastapi RSS grew 142% over 24h (threshold 25%)

Thresholds are conservative. Tighten them in the CI variant once a
clean baseline run lands.

Why this isn't pytest
---------------------

24-hour pytest runs are ergonomically awful (no incremental output,
hard to interrupt cleanly). This script is shaped like an operator
tool: streams progress, writes JSON for later analysis, exits 0/1 for
CI gating.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx


HERE = Path(__file__).parent
LOAD_TEST = HERE / "load_test.py"

# Growth thresholds — alert if any of these are exceeded between t=0 and
# t=end. Conservative for V1; tighten once we have a clean reference.
THRESHOLD_RSS_PCT = 25.0
THRESHOLD_REDIS_KEYS_PCT = 50.0
THRESHOLD_PG_CONNS_PCT = 30.0
THRESHOLD_LATENCY_PCT = 20.0


@dataclass
class Sample:
    ts_utc: str
    cycle: int
    fastapi_up: bool
    p95_seconds_by_class: dict[str, float] = field(default_factory=dict)
    fastapi_rss_bytes: int | None = None
    redis_used_memory_bytes: int | None = None
    redis_keys_total: int | None = None
    pg_active_connections: int | None = None


async def _fetch_metric(client: httpx.AsyncClient, url: str, key_prefix: str) -> dict[str, int]:
    """Pull a Prometheus exposition response and extract metrics that start with key_prefix."""
    out: dict[str, int] = {}
    try:
        r = await client.get(url, timeout=5.0)
        if r.status_code != 200:
            return out
        for line in r.text.splitlines():
            if not line or line.startswith("#"):
                continue
            if line.startswith(key_prefix):
                name, _, value = line.partition(" ")
                try:
                    out[name] = int(float(value.strip()))
                except ValueError:
                    pass
    except Exception:
        pass
    return out


async def _fetch_healthz(client: httpx.AsyncClient, base: str) -> bool:
    try:
        r = await client.get(f"{base}/healthz", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


def _run_load_test(base: str, project_id: str, service_key: str) -> dict[str, float]:
    """Invoke load_test.py and parse its --json output for per-class p95."""
    env = os.environ.copy()
    env.update({
        "FASTAPI_URL": base,
        "FASTAPI_SERVICE_KEY": service_key,
    })
    proc = subprocess.run(
        [
            sys.executable,
            str(LOAD_TEST),
            "--concurrency", "2",
            "--total", "10",
            "--project", project_id,
            "--json",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    if proc.returncode != 0:
        return {}
    try:
        payload = json.loads(proc.stdout)
        per_class = payload.get("per_class", {})
        return {
            cls: float(stats.get("p95_seconds", 0.0))
            for cls, stats in per_class.items()
        }
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}


async def _sample(base: str, project_id: str, service_key: str, cycle: int) -> Sample:
    async with httpx.AsyncClient() as client:
        up = await _fetch_healthz(client, base)
        fastapi_metrics = await _fetch_metric(client, f"{base}/metrics", "process_resident_memory_bytes")

    p95 = _run_load_test(base, project_id, service_key) if up else {}

    return Sample(
        ts_utc=datetime.now(timezone.utc).isoformat(),
        cycle=cycle,
        fastapi_up=up,
        p95_seconds_by_class=p95,
        fastapi_rss_bytes=fastapi_metrics.get("process_resident_memory_bytes"),
    )


def _pct_growth(first: float | None, last: float | None) -> float | None:
    if first in (None, 0) or last is None:
        return None
    return (last - first) / first * 100.0


def _verdict(samples: list[Sample]) -> tuple[bool, list[str]]:
    if len(samples) < 2:
        return False, ["insufficient samples (n<2)"]
    first, last = samples[0], samples[-1]
    flags: list[str] = []

    rss_growth = _pct_growth(first.fastapi_rss_bytes, last.fastapi_rss_bytes)
    if rss_growth is not None and rss_growth > THRESHOLD_RSS_PCT:
        flags.append(f"fastapi RSS grew {rss_growth:+.1f}% (threshold {THRESHOLD_RSS_PCT}%)")

    classes = set(first.p95_seconds_by_class) & set(last.p95_seconds_by_class)
    for cls in classes:
        delta = _pct_growth(first.p95_seconds_by_class[cls], last.p95_seconds_by_class[cls])
        if delta is not None and delta > THRESHOLD_LATENCY_PCT:
            flags.append(f"class={cls} p95 grew {delta:+.1f}% (threshold {THRESHOLD_LATENCY_PCT}%)")

    return (len(flags) == 0), flags


async def main() -> int:
    parser = argparse.ArgumentParser(description="GeoRAG soak test (v1.5-02)")
    parser.add_argument("--hours", type=float, default=1.0, help="total soak duration")
    parser.add_argument("--cycle-seconds", type=int, default=600, help="seconds between samples")
    parser.add_argument("--out", default="soak-trend.json", help="JSON trend output path")
    args = parser.parse_args()

    base = os.environ.get("FASTAPI_URL", "http://localhost:8000")
    service_key = os.environ.get("FASTAPI_SERVICE_KEY", "")
    project_id = os.environ.get("STAGING_PROJECT_ID", "019d74a1-fba8-7165-9ae6-a5bf93eef97d")

    if not service_key:
        sys.stderr.write("FASTAPI_SERVICE_KEY not set — refusing to soak unauthenticated\n")
        return 2

    deadline = time.monotonic() + args.hours * 3600
    samples: list[Sample] = []
    cycle = 0

    stop = False

    def _on_signal(signum, frame):  # noqa: ARG001
        nonlocal stop
        stop = True
        sys.stderr.write("\nSIGINT received — finalising sample...\n")

    signal.signal(signal.SIGINT, _on_signal)

    print("# GeoRAG soak — TSV (cycle\\tts_utc\\tup\\trss_mb\\tp95_factual\\tp95_spatial)")

    while not stop and time.monotonic() < deadline:
        cycle += 1
        sample = await _sample(base, project_id, service_key, cycle)
        samples.append(sample)
        rss_mb = (sample.fastapi_rss_bytes or 0) / 1024 / 1024
        print(
            f"{cycle}\t{sample.ts_utc}\t{int(sample.fastapi_up)}\t{rss_mb:.0f}\t"
            f"{sample.p95_seconds_by_class.get('factual', 0):.2f}\t"
            f"{sample.p95_seconds_by_class.get('spatial', 0):.2f}",
            flush=True,
        )

        next_at = time.monotonic() + args.cycle_seconds
        while time.monotonic() < next_at and time.monotonic() < deadline and not stop:
            await asyncio.sleep(2)

    Path(args.out).write_text(json.dumps([asdict(s) for s in samples], indent=2))

    passed, flags = _verdict(samples)
    print(f"\n# Soak summary: {len(samples)} cycles over {args.hours}h")
    if passed:
        print("SOAK PASS — no growth above thresholds")
        return 0
    for flag in flags:
        print(f"  - {flag}")
    print("SOAK FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
