"""V1.5-07 — Neo4j JMX → Prometheus exporter.

Bridges Neo4j 2026 Community Edition's JMX surface to Prometheus.
Community Edition can't expose `/metrics` natively (the
`server.metrics.prometheus.*` config keys are Enterprise-gated and
rejected at startup), so this sidecar polls `dbms.queryJmx` over Bolt
every 30s and serves Prometheus exposition format on :9105/metrics.

Metric surface (Community Edition — see "Limits" below)
--------------
- neo4j_up                                gauge   1 if last poll succeeded
- neo4j_heap_used_bytes                   gauge   java.lang:type=Memory HeapMemoryUsage.used
- neo4j_heap_max_bytes                    gauge   java.lang:type=Memory HeapMemoryUsage.max
- neo4j_heap_committed_bytes              gauge   ... HeapMemoryUsage.committed
- neo4j_threads                           gauge   java.lang:type=Threading ThreadCount
- neo4j_threads_peak                      gauge   ... PeakThreadCount
- neo4j_threads_daemon                    gauge   ... DaemonThreadCount
- neo4j_uptime_seconds                    gauge   java.lang:type=Runtime Uptime / 1000
- neo4j_open_file_descriptors             gauge   java.lang:type=OperatingSystem OpenFileDescriptorCount
- neo4j_max_file_descriptors              gauge   ... MaxFileDescriptorCount

Limits (Neo4j 2026 Community Edition)
-------------------------------------
The richer `org.neo4j:*` JMX namespace (page cache hits/faults, bolt
connection counters, transaction throughput) is **Enterprise-only**.
Community Edition exposes only `java.lang:*` JVM-level beans, which is
why this exporter's surface is JVM-flavoured rather than Neo4j-flavoured.
Upgrading to Enterprise OR running cypher queries against the database
itself (e.g. count nodes per label as a proxy for traffic) is the
follow-up if richer metrics are needed.

Why hand-rolled
---------------
Existing community exporters either require Enterprise (uses /metrics) or
are unmaintained. This module is ~100 lines of Python; cheaper than
auditing a third-party container for free-licensing fit.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from neo4j import GraphDatabase

logging.basicConfig(
    format='{"timestamp":"%(asctime)s","level":"%(levelname)s","logger":"neo4j_exporter","message":"%(message)s"}',
    level=logging.INFO,
)
logger = logging.getLogger("neo4j_exporter")

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://neo4j:7687")
NEO4J_USER = os.environ.get("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")
POLL_INTERVAL_S = int(os.environ.get("NEO4J_EXPORTER_POLL_INTERVAL_S", "30"))
LISTEN_PORT = int(os.environ.get("NEO4J_EXPORTER_PORT", "9105"))

# Shared state — written by the polling thread, read by HTTP handler.
# A simple dict + threading.Lock is enough; we're emitting <2 KB per poll.
_STATE_LOCK = threading.Lock()
_LATEST_METRICS: dict[str, float] = {}
_LATEST_TS: float = 0.0
_LAST_POLL_OK: bool = False


def _query_jmx(session, bean: str, attribute: str) -> Any:
    """Pull a single JMX bean attribute. Returns None on miss.

    Neo4j 2026 wraps every attribute as
        {description: "...", value: <scalar>}
    Composite attributes (like HeapMemoryUsage) wrap the value AGAIN as
        {description: "...", value: {description: "...", properties: {...}}}
    so we need to peel both layers.
    """
    result = session.run(
        "CALL dbms.queryJmx($bean) YIELD attributes RETURN attributes",
        bean=bean,
    )
    record = result.single()
    if record is None:
        return None
    attrs = record["attributes"] or {}
    if attribute not in attrs:
        return None
    val = attrs[attribute]
    if isinstance(val, dict) and "value" in val:
        inner = val["value"]
        # Composite — return the `properties` dict so the caller can index
        # into `used`, `max`, etc.
        if isinstance(inner, dict) and "properties" in inner:
            return inner["properties"]
        return inner
    return val


def _poll_once() -> None:
    """One scrape cycle — pull JMX beans, update _LATEST_METRICS."""
    global _LATEST_TS, _LAST_POLL_OK

    metrics: dict[str, float] = {}
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        try:
            with driver.session() as session:
                # Heap memory (java.lang:type=Memory composite attribute).
                heap = _query_jmx(session, "java.lang:type=Memory", "HeapMemoryUsage")
                if isinstance(heap, dict):
                    if "used" in heap:
                        metrics["neo4j_heap_used_bytes"] = float(heap["used"])
                    if "max" in heap and heap["max"] >= 0:
                        metrics["neo4j_heap_max_bytes"] = float(heap["max"])
                    if "committed" in heap:
                        metrics["neo4j_heap_committed_bytes"] = float(heap["committed"])

                # Threads (java.lang:type=Threading scalar attributes).
                count = _query_jmx(session, "java.lang:type=Threading", "ThreadCount")
                if count is not None:
                    metrics["neo4j_threads"] = float(count)
                peak = _query_jmx(session, "java.lang:type=Threading", "PeakThreadCount")
                if peak is not None:
                    metrics["neo4j_threads_peak"] = float(peak)
                daemon = _query_jmx(session, "java.lang:type=Threading", "DaemonThreadCount")
                if daemon is not None:
                    metrics["neo4j_threads_daemon"] = float(daemon)

                # Uptime (java.lang:type=Runtime Uptime in milliseconds).
                uptime = _query_jmx(session, "java.lang:type=Runtime", "Uptime")
                if uptime is not None:
                    metrics["neo4j_uptime_seconds"] = float(uptime) / 1000.0

                # File descriptors (Linux java.lang:type=OperatingSystem).
                ofd = _query_jmx(
                    session, "java.lang:type=OperatingSystem", "OpenFileDescriptorCount"
                )
                if ofd is not None:
                    metrics["neo4j_open_file_descriptors"] = float(ofd)
                mfd = _query_jmx(
                    session, "java.lang:type=OperatingSystem", "MaxFileDescriptorCount"
                )
                if mfd is not None:
                    metrics["neo4j_max_file_descriptors"] = float(mfd)
        finally:
            driver.close()
        _LAST_POLL_OK = True
        logger.info("poll_ok metrics=%d", len(metrics))
    except Exception as exc:
        _LAST_POLL_OK = False
        logger.warning("poll_failed err=%s", str(exc))

    metrics["neo4j_up"] = 1.0 if _LAST_POLL_OK else 0.0

    with _STATE_LOCK:
        _LATEST_METRICS.clear()
        _LATEST_METRICS.update(metrics)
        _LATEST_TS = time.time()


def _poll_loop() -> None:
    """Background thread — poll every POLL_INTERVAL_S seconds."""
    while True:
        _poll_once()
        time.sleep(POLL_INTERVAL_S)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


_HELP = {
    "neo4j_up": ("gauge", "1 if the last JMX poll succeeded"),
    "neo4j_heap_used_bytes": ("gauge", "JVM heap bytes in use"),
    "neo4j_heap_max_bytes": ("gauge", "JVM heap bytes available"),
    "neo4j_heap_committed_bytes": ("gauge", "JVM heap bytes committed by the OS"),
    "neo4j_threads": ("gauge", "Live JVM threads"),
    "neo4j_threads_peak": ("gauge", "Peak live JVM threads since process start"),
    "neo4j_threads_daemon": ("gauge", "Live daemon JVM threads"),
    "neo4j_uptime_seconds": ("gauge", "Process uptime in seconds"),
    "neo4j_open_file_descriptors": ("gauge", "Open file descriptors held by the JVM"),
    "neo4j_max_file_descriptors": ("gauge", "Hard ulimit for open file descriptors"),
}


def _exposition_lines() -> list[str]:
    with _STATE_LOCK:
        snapshot = dict(_LATEST_METRICS)

    out: list[str] = []
    for name, value in snapshot.items():
        if name in _HELP:
            kind, doc = _HELP[name]
            out.append(f"# HELP {name} {doc}")
            out.append(f"# TYPE {name} {kind}")
        out.append(f"{name} {value:g}")
    return out


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = ("\n".join(_exposition_lines()) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802
        # Quiet the default access log; the JSON logger above is enough.
        pass


def main() -> None:
    poll_thread = threading.Thread(target=_poll_loop, daemon=True, name="neo4j-poller")
    poll_thread.start()

    server = HTTPServer(("0.0.0.0", LISTEN_PORT), _Handler)
    logger.info("listening on 0.0.0.0:%d", LISTEN_PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
