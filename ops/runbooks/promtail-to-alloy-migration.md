# Promtail → Grafana Alloy migration

> **Status**: Plan only. Migration is not executed yet.
> **Last updated**: 2026-06-23

## Why

Grafana publicly deprecated **Promtail** in 2026 in favour of **Grafana Alloy**. Tag publication for `grafana/promtail` is frozen at the **3.5.x** line — the version pin in `docker-compose.yml` is the terminal Promtail release we'll ever ship. Loki itself continues to receive updates (currently 3.7.2), and Promtail's push API remains forward-compatible against Loki 3.x — so we have a **stable but unmaintained logging shipper** today. That's fine indefinitely from a wire-protocol perspective but accumulates risk:

1. **No security patches.** If a CVE surfaces in Promtail or its deps, there's no upstream fix coming.
2. **No new features.** Grafana's investment in log-shipping ergonomics, OTel collector convergence, and Profiles correlation all lands in Alloy.
3. **Doc / config rot.** Grafana docs increasingly point to Alloy-only examples; finding Promtail-specific answers gets harder over time.

Alloy is the strategic destination — it merges Promtail, the OTel collector, and Pyroscope's agent into one binary with one config format. We already run an OTel collector for traces, so Alloy is a candidate for **consolidating two agents into one** as a side benefit.

## What "Alloy" means in practice

| Today | After migration |
|---|---|
| `grafana/promtail:3.5.5` container scraping Docker stdout, pushing to `http://loki:3100/loki/api/v1/push`. Config in `docker/promtail/promtail.yaml` (YAML). | `grafana/alloy:<pinned>` container running the same Docker-stdout scrape + Loki push. Config in `docker/alloy/config.alloy` (River syntax — Alloy's HCL-like format). |
| Separate `otel/opentelemetry-collector-contrib:0.154.0` container handling traces (OTLP → Tempo). Config in `docker/otel-collector/config.yaml`. | **Optional consolidation**: same Alloy instance can host the OTLP receiver + Tempo exporter. Drops one container; one config file to maintain. |

Both options are reasonable. The minimal migration is "Alloy as drop-in Promtail replacement"; the full consolidation is "Alloy as unified telemetry agent." Sequencing decision deferred to the migration session.

## Pre-migration checklist

- [ ] **Verify Alloy version compatibility with Loki 3.7.x.** Pull the latest stable Alloy and confirm the loki.write component speaks the Loki 3.7.x push protocol cleanly.
- [ ] **Inventory the existing Promtail scrape config.** Currently scrapes Docker container stdout via the `docker_sd_configs` mechanism; relabels using container labels. Translate every relabel rule into Alloy's `discovery.relabel` + `loki.process` syntax.
- [ ] **Test in shadow mode.** Run Promtail + Alloy in parallel writing to the same Loki instance (different tenant IDs or stream labels for separation) for one week. Compare:
  - Lines/sec ingested per container (should match within 0.1%).
  - Label cardinality emitted (should match exactly).
  - Promtail-specific custom labels (e.g., `authz_audit` channel routing) must be reproduced.
- [ ] **Decide on consolidation scope.** Minimal (logs only) or full (logs + traces)? If full, also translate `docker/otel-collector/config.yaml` into Alloy `otelcol.*` components.
- [ ] **Update alert rules.** Any alerts that pivot on Promtail process metrics (`promtail_*` series in `docker/prometheus/rules/`) need to be re-pointed at Alloy's exposition (`alloy_*` series).

## Migration steps

> Each step is a separate commit / PR for surgical rollback.

### Step 1 — Add the Alloy container alongside Promtail

In `docker-compose.yml`, add a new service `alloy` mirroring the Promtail volume mounts. Same Docker socket access, same `promtail_positions` named volume (or equivalent for Alloy's state). New service first writes to a separate Loki tenant so we can A/B-compare without polluting the canonical log stream.

### Step 2 — Translate the Promtail config to Alloy

`docker/promtail/promtail.yaml` → `docker/alloy/config.alloy`. The Promtail->Alloy syntax converter at `alloy convert --source-format=promtail` is the canonical starting point. Hand-edit the output for any Alloy-only ergonomic wins (e.g., the unified `loki.process` pipeline replaces several separate `pipeline_stages` blocks).

### Step 3 — Shadow run + verify

Both ship for a week. Diff line counts + label cardinality in Loki. The `authz_audit` Monolog channel + the W3C trace-id stamping must come through Alloy intact.

### Step 4 — Cut over to Alloy

Stop Promtail; promote Alloy from the shadow tenant to the canonical tenant. Keep Promtail service definition in compose (commented out) for one week as fast rollback.

### Step 5 — Optional consolidation (separate session)

If proceeding with full consolidation, translate `docker/otel-collector/config.yaml` into Alloy components. OTel collector container can then be removed.

### Step 6 — Cleanup

Remove the commented-out Promtail block, delete `docker/promtail/`, remove the Promtail service health endpoint from any dashboards, update `docs/handover/` references.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| **Relabel rule semantics drift.** Alloy's relabel component has slightly different defaults around empty-label handling. | Shadow-run comparison catches mismatches before promote. |
| **`positions.yaml` format incompatibility.** Promtail tracks tail offsets in a YAML file; Alloy uses a different format. A fresh Alloy starts re-tailing from the beginning of every container's stdout. | Acceptable for a shadow window (some duplicate lines briefly); plan the promote during a low-volume window. |
| **Loki tenant header drift.** Currently no tenant header (single-tenant Loki); the shadow run uses tenant IDs for separation, so promote needs to drop the tenant header. | Document the env-var flip clearly in the cutover step. |
| **Operator muscle memory.** Anyone debugging "is Promtail healthy?" needs to learn the Alloy debug endpoints (`/debug/pprof`, `/-/ready`, etc.). | Update `ops/runbooks/log-retention.md` + add a quick-reference card during migration. |

## Decision points the migration session needs to resolve

1. **Minimal migration vs full consolidation.** Do we collapse OTel into Alloy at the same time?
2. **Alloy version pin.** Pick the current stable + capture digest at migration time.
3. **State volume.** New `alloy_state` named volume or reuse `promtail_positions`?
4. **Rollback window.** How long to keep Promtail's commented-out service block before removal?

## References

- Grafana Alloy docs: https://grafana.com/docs/alloy/latest/
- Promtail deprecation announcement: tracked in `docs/handover/REPORT_GAP_ANALYSIS.md`.
- Current Promtail config: `docker/promtail/promtail.yaml`.
- Current OTel config: `docker/otel-collector/config.yaml`.
- 2026-06 audit punch-list item 1 (Promtail → Alloy).
