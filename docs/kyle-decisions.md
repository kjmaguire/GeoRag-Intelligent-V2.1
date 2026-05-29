# Kyle-Decision Items (v1.5-22)

Capture of architectural decisions deferred to owner sign-off during V1
production-hardening (Modules 1-10) and V1.5 sweep. Each item lists the
current state, the options, and the cost of each path. Items are
**not** open bugs — they are choices where engineering can defend either
direction and needs the project owner to pick.

When a decision is made, update the architecture doc and the relevant
status memos, then move the item below the `## Resolved` heading at the
bottom of this file.

---

## D1 — Qdrant `ef_construct`: 200 (live) vs 128 (original spec)

**Current state:** All 5 live Qdrant collections were built with
`ef_construct=200`. Architecture doc §06 was updated 2026-04-26 (v1.5-21)
to document the live value with a recall/cost note.

**Options:**

- **(a) Keep 200, codify as canonical.** No-op — the doc and the live
  state already agree. Higher recall, higher index build time. No
  collection rebuild needed.
- **(b) Drop to 128, schedule rebuild.** Faster index builds; possible
  recall cost (depends on corpus). Requires:
  - Module 4 baseline measurement before drop (ensure recall@5 doesn't
    regress by more than the perf-baseline threshold).
  - Coordinated rebuild of all 5 collections during a maintenance
    window — `georag_reports`, `pg_drillhole_collar`, `pg_mine`,
    `pg_mineral_occurrence`, `pg_resource_potential_zone`.
  - Re-embed every passage and re-upsert.

**Recommendation:** (a). The recall/cost tradeoff is small at this
corpus size and a rebuild is operator effort with no clear benefit.
Revisit if a future corpus pushes index build over ~1 hour.

**Owner decision:** _pending_

---


## D3 — Dagster daemon GPU access for SPLADE inference

**Current state:** SPLADE++
(`naver/splade-cocondenser-ensembledistil`) runs on CPU inside
`georag-dagster-daemon`. CPU rate ≈ 32 texts / 7 s batch. The full
`index_public_geoscience_qdrant` run (56,767+ rows × 6 collections)
takes 4–5 hours wall time. Dev-acceptable.

**Options at production deploy:**

- **(a) Add NVIDIA device reservation to dagster-daemon.** Requires
  nvidia-container-toolkit on the host. Cuts SPLADE inference to
  minutes. Couples dagster-daemon to GPU availability — host must have
  a CUDA-capable GPU.
- **(b) Pre-compute sparse embeddings offline.** Add a Bronze→Silver
  step that emits SPLADE vectors into a PG column; the index asset
  then only does upserts, no inference. Cleaner medallion separation,
  no GPU coupling at the Dagster layer. Cost: one new asset + a
  re-ingestion pass.
- **(c) Accept CPU rates in prod for V1.** 4–5 hours per full reindex
  is fine for monthly/quarterly refreshes. Defer (a) or (b) until a
  client demands it.

**Recommendation:** (c) for V1; (b) when scale justifies it.

**Owner decision:** _pending_

---

## D4 — Neo4j heap initial size restart timing

**Current state:** `NEO4J_server_memory_heap_initial__size=4G` is set
in `docker-compose.yml`. The live JVM was started with `initial=2G`
before the env was added — it has not consumed the new value because
no restart has happened.

**Options:**

- **(a) Restart in next backup window.** The weekly Neo4j backup window
  already stops Neo4j for the dump; let it pick up `initial=4G` on
  restart. Zero extra downtime.
- **(b) Restart now.** Minimal user impact in dev. Verify post-restart
  with: `docker exec georag-neo4j bash -c "ps aux | grep java" | grep -oE '\-Xms[0-9]+[gGmM]'`

**Recommendation:** (a). Operator convenience; no functional gain from
doing it sooner.

**Owner decision:** _pending_

---

## D5 — Neo4j retrieval timeout: 2.0 s (code) vs 3.0 s (spec §06)

**Current state:** `orchestrator.py` sets `TIMEOUT_NEO4J_S = 2.0`.
Architecture §06 specifies 3.0 s. Empirically the indexed graph
returns within 2 s on the demo corpus.

**Options:**

- **(a) Update spec → 2.0 s.** Code is empirically fine; doc-only edit.
- **(b) Tune code → 3.0 s.** Buys headroom for larger graphs / cold
  page cache after restart. Costs up to 1 s of tail latency on a
  graph timeout.

**Recommendation:** (a) for V1 demo corpus. Revisit (b) if the field
graph grows past ~1 M nodes or page-cache misses become measurable.

**Owner decision:** _pending_

---

## D6 — Architecture-doc addendum sweep

**Current state:** v1.5-21 (2026-04-26) reconciled the base
`georag-architecture.html` to v1.10. Several edits from
`ops/backlog/module-10-doc-sweep.md` were skipped because the target
text lives in addendum sections (e.g. §04j evidence-model FK
cascades, §04j `bronze.provenance` / `document_revisions` coexistence,
B8 enable-order, the Qdrant `sparse_vectors_config` PATCH example,
explicit query-class precedence list). These need a separate
addendum-doc pass.

**Options:**

- **(a) Author addendum doc inline in `georag-architecture.html`.**
  Append §04j and friends as new sections. Single source of truth.
- **(b) Keep addendum content in `ops/backlog/module-10-doc-sweep.md`
  and audit files.** Architecture doc stays the high-level overview;
  detailed schemas live in module-spec docs.

**Recommendation:** (b) for V1. The audit and backlog files are
already source-of-truth for these details, and folding them into the
HTML doc grows it past the point where it serves as a quick reference.

**Owner decision:** _pending_

---

## Resolved

- **D1 → option (a) "keep 200, codify as canonical"** (2026-04-26).
  Architecture §06 documents `ef_construct=200` with the recall/cost
  rationale; no collection rebuild scheduled. Revisit if a future
  corpus pushes index build time over ~1 hour.
- **D3 → option (c) "accept CPU rates for V1"** (2026-04-26). Dev
  4–5 hour reindex is acceptable for monthly/quarterly refresh.
  Revisit (option b — pre-compute Bronze→Silver) when scale demands.
- **D4 → option (a) "restart in next backup window"** (2026-04-26).
  Zero extra downtime; weekly Neo4j backup window will pick up the
  4G initial-heap value.
- **D5 → option (a) "spec matches code at 2.0 s"** (2026-04-26).
  No 3.0 s timeout text was present in `georag-architecture.html`
  base doc — the drift was tracked in
  `ops/backlog/module-10-doc-sweep.md` only. Code (2.0 s) is now
  canonical. If §06 of any future addendum doc surfaces an explicit
  timeout, align to 2.0 s.
- **D6 → option (b) "keep addendum content in audit/backlog files"**
  (2026-04-26). Architecture HTML stays the high-level overview;
  detailed §04j evidence-model FK semantics and `bronze.provenance`
  vs `document_revisions` distinction remain authoritative in
  `ops/backlog/module-10-doc-sweep.md` and the Module 3 review file.
- **D2 → option (a) "rename live to match spec"** (2026-04-27).
  Migration prepared by graph-engineer: all code paths updated from
  `:Drillhole` to `:DrillHole`, migration script authored at
  `ops/migrations/neo4j/2026-04-27-drillhole-rename.cypher`, runbook
  at `ops/runbooks/drillhole-label-rename.md`. Operator applies the
  migration during next maintenance window per the runbook.

---

*Authored 2026-04-26 during V1.5-22 close-out. Update when decisions
are made or new owner-decision items are flagged.*
