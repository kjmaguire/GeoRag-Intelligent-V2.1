# V1.5-07 — Neo4j JMX → Prometheus exporter sidecar.
# Polls Neo4j 2026 Community Edition's JMX surface via the bolt protocol
# and serves Prometheus exposition format on :9105/metrics.
#
# Why a sidecar: Community Edition rejects `server.metrics.prometheus.*`
# at startup (Enterprise-only). This is the supported workaround per
# our backlog item v1.5-07.

FROM python:3.13-slim

LABEL org.opencontainers.image.title="GeoRAG Neo4j Exporter"
LABEL org.opencontainers.image.description="JMX → Prometheus bridge for Neo4j Community Edition (V1.5-07)"

# Run as nobody (UID 65534) — matches the convention from Module 9 9.7.
# No filesystem writes needed beyond /tmp; no privileged ports.

WORKDIR /app

# Single dep: the official Neo4j Python driver. Apache-2.0 licensed.
RUN pip install --no-cache-dir 'neo4j>=5.27,<6.0'

COPY exporter.py /app/exporter.py

USER 65534:65534

EXPOSE 9105

# No HEALTHCHECK directive — the compose `healthcheck:` block invokes
# /metrics directly, which is the canonical liveness signal for this
# sidecar.

CMD ["python", "-u", "/app/exporter.py"]
