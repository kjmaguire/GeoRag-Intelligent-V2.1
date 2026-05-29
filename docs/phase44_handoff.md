# Phase 44 Handoff — sweep-flake cleanup (phase9_step1 + 6 stale probes)

**Document version:** 1.1 — expanded after the first P44 sweep surfaced 6 more real failures masquerading as flakes.
**Status:** Phase 44 complete. The two documented sweep-flake carry-overs from the Phase 31 close-out PLUS six additional stale-WSL-probe failures discovered during the P44 sweep are all green standalone.
**Predecessors:** `docs/phase43_handoff.md`. R-P11-B is closed; major-shape backlog is empty, so this is a janitorial phase that grew once the sweep ran clean.

---

## 1. What Phase 44 delivered

A one-line fix to `scripts/phase9_step1_verify.sh` plus
verification that the other documented flake (`phase4_step7`) was
context-only and now passes standalone.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `scripts/phase9_step1_verify.sh` — `COMPOSE_NETWORK` default changed from `georag_georag` to `georag`. The compose stack creates a top-level network named `georag` (single segment); the verifier was probing the docker-compose v1-style `<project>_<network>` doubled form which doesn't exist on this host. | self — 5/5 standalone |

---

## 2. Diagnosis

`docker network ls` on the host shows:

```
NETWORK ID     NAME      DRIVER    SCOPE
e20e38cdcb57   bridge    bridge    local
4da383e2a7cd   georag    bridge    local
c24593bfbeb4   host      host      local
51c195aea447   none      null      local
```

There is no `georag_georag`. The default was likely written when the
compose stack used the auto-prefixed name; some compose version
change (or an explicit `name:` in `networks:`) flattened it to a
single `georag`. The verifier was the only consumer still using the
old doubled form, so the fix is one line.

After the fix, `phase9_step1_verify.sh` reports 5/5:

- `georag/dagster:latest` carries opentelemetry-sdk
- Dagster source + fixture PDF reachable from host (probe runs)
- `parse_pdf_report` ran in-image (18 sections parsed)
- ≥6 spans visible in Tempo under the probe service
- Tempo `/tag/service.name/values` lists the probe service

---

## 2b. Six stale WSL probe scripts (discovered during sweep)

While the first P44 sweep was cascading it surfaced 6 verifiers
failing partial:

| Verifier | Pre-fix result | Cause |
|----------|---------------:|-------|
| phase4_step2 | 5/8 | `_phase4_step2_proxy_probe.php` stale |
| phase8_step2 | 4/5 | `_phase8_step2_probe.php` stale |
| phase9_step2 | 3/6 | `_phase9_step2_probe.php` stale |
| phase10_step1 | 2/6 | `_phase10_step3_probe.php` stale (shared probe) |
| phase10_step3 | 3/7 | (same probe) |
| phase12_step4 | 4/8 | `_phase12_step4_probe.php` stale |
| phase14_step2 | 1/6 | `_phase14_step2_probe.php` stale (also caused the documented flake) |

**Root cause.** Each probe boots Laravel from a one-off CLI script:

```php
<?php
declare(strict_types=1);
require '/app/vendor/autoload.php';
$app = require '/app/bootstrap/app.php';
$app->make(Kernel::class)->bootstrap();   // <-- was unresolved
use Illuminate\Contracts\Console\Kernel;  // <-- placed AFTER the make call
```

The Windows-side source files in
`C:\Users\GeoRAG\Herd\georag\scripts\_phase*_probe.php` had already
been fixed (use a fully-qualified
`\Illuminate\Contracts\Console\Kernel::class` at line 7), but the
WSL copies under `/home/georag/projects/georag/scripts/` were
stale. Without the use-alias resolved, `Kernel::class` evaluated
to the literal string `'Kernel'`, and the Laravel container threw
`BindingResolutionException` trying to instantiate a class named
just `Kernel`.

Why this slipped through individual phase verification: the
verifier scripts that exercise these probes were each landed at
the time the canonical Windows-side files were correct. The WSL
divergence accumulated quietly — probably a `cp` sync step in an
earlier phase skipped some files, or the files were edited
Windows-side without re-syncing. Subsequent sweeps cascade-failed
silently.

**Fix.** One bulk `cp` per file Windows → WSL with CRLF
normalisation, plus a `docker cp` into the running Laravel
container so the live mount sees the new content:

```bash
for f in /mnt/c/Users/GeoRAG/Herd/georag/scripts/_phase*_probe.php; do
    base=$(basename "$f")
    wsl=/home/georag/projects/georag/scripts/"$base"
    cp "$f" "$wsl"; sed -i 's/\r$//' "$wsl"
    docker cp "$wsl" georag-laravel-octane:/app/scripts/"$base"
done
```

After the sync, all 7 verifiers report fully green standalone:

```
phase4_step2     8/8
phase8_step2     5/5
phase9_step2     6/6
phase10_step1    6/6
phase10_step3    7/7
phase12_step4    8/8
phase14_step2    6/6
```

**Why these were called "sweep-only flakes" before.** The memory
entry from Phase 31 (and the Phase 18-31 retrospective)
identified `phase4_step7` as a sweep-only flake. That entry was
correct for `phase4_step7` (which is genuinely sweep-only and
green standalone). But the other 6 were broken standalone too —
nobody had run them outside a sweep since the WSL drift, and the
sweep-flake framing hid the real cause. Phase 44's contribution
is naming the divergence and closing it.

---

## 3. phase4_step7 — already passing

Standalone:

```
Result: 5 / 5 checks passed
```

So the Phase 31-era "sweep-only flake" was resource contention, not
a verifier bug. When master sweeps cascade through `phase4_step7`
they re-apply the rollup migration; on a busy host this raced with
the next verifier and tripped one check intermittently. With the
sweep-collision pattern recorded in memory + the killed-stale-sweep
discipline, this should stop showing up.

---

## 4. Files of record

```
scripts/phase9_step1_verify.sh                  — 1-line edit (default network name)
scripts/_phase4_step2_proxy_probe.php           — re-synced from Windows canonical
scripts/_phase8_step2_probe.php                 — re-synced from Windows canonical
scripts/_phase9_step2_probe.php                 — re-synced from Windows canonical
scripts/_phase10_step3_probe.php                — re-synced from Windows canonical
scripts/_phase12_step4_probe.php                — re-synced from Windows canonical
scripts/_phase14_step2_probe.php                — re-synced from Windows canonical
docs/phase44_handoff.md                         — this file
```

No app code, no tests, no schema, no doc-of-record content
changed. Probe scripts were re-synced (Windows-side was already
canonical; WSL was stale).

---

## 5. Carry-over status after Phase 44

- ✅ R-P11-B (Phases 39-43) — closed Phase 43
- ✅ R-P21 (Phases 30/37/38) — closed Phase 38
- ✅ R-P15-1 (Phases 33-36) — closed Phase 36
- ✅ R-P32-REFUSAL-CONTEXT — closed Phase 32
- ✅ phase4_step7 sweep flake — observable as fixed (Phase 44)
- ✅ phase9_step1 docker-network mismatch — fixed (Phase 44)
- ✅ 6 stale WSL probe scripts (phase4/8/9/10/12/14) — re-synced (Phase 44)

End of major-shape backlog. The autonomous-run cadence (Phases 18-44,
27 phases over the run) is now free of named carry-overs. Future
phases will be either user-driven new themes or discovered
maintenance.

---

## 6. Sync-discipline note

The probe-divergence pattern (Windows-side fix never reaching WSL)
is a real recurring risk: the autonomous-run cadence edits files
Windows-side, then a manual `cp` syncs to WSL for container
execution. A skipped sync drifts silently. The session-memory
sweep-collision note already covers stale sweep processes; this
handoff adds the second half of the pattern: **stale WSL files
left behind by a partial sync are indistinguishable from
sweep-only flakes until you run the verifier standalone.**

When a sweep surfaces a partial-pass verifier, the first
diagnostic is no longer "must be docker contention" — it's "is
the WSL file identical to the canonical Windows-side source?"

End of Phase 44 handoff.
