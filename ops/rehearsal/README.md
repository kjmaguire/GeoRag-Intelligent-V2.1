# Go-live rehearsal corpus

A two-tenant corpus for exercising the live AWS deployment, and the
verification that goes with it. Built during the 2026-09-18 rehearsal.

## What this is for

`database/tests/pgtap/08_silver_mvt_functions.sql` tests 73-77 already prove
the Martin tenant fence is **correct**, with both positive controls present,
and they run on every PR. Nothing here replaces that.

These files answer the two questions CI structurally cannot:

1. Does the RDS instance this platform actually runs on **carry** the fenced
   function definitions — i.e. was `2026_09_16_120000` applied here, or
   merely merged?
2. Does the fence hold for workspaces created the ordinary way, rather than
   for a fixture built by the same commit as the function it tests?

## The one design decision that matters

**The two tenants are co-located.** Their collars interleave 100 m apart
along a single line rather than sitting in separate regions.

Give tenant A collars in British Columbia and tenant B collars in
Saskatchewan and every cross-tenant assertion passes whether or not the fence
works — the bounding box already excluded the other tenant's rows. The suite
goes green and proves nothing, and it looks exactly like a suite that proves
something. Here, any tile containing one tenant's collar contains the
other's, so `workspace_id` is the only thing that can separate them.

`verify_tenant_fence.sql` section 1 asserts that co-location rather than
assuming it, so editing the seed coordinates breaks the run loudly instead of
quietly turning sections 2-4 into no-ops. That guard is load-bearing: with
the tenants separated *and* both fence guards removed, the script still
refuses to report a pass.

## Files

| file | |
|---|---|
| `seed_multitenant_corpus.sql` | Two workspaces, two projects, six interleaved collars. Idempotent, deterministic ids. |
| `verify_tenant_fence.sql` | Four sections; every check RAISEs, so psql's exit code is the verdict. |
| `teardown_multitenant_corpus.sql` | Deletes exactly the seeded ids — not a `LIKE 'rehearsal-%'` sweep. |
| `run_against_deployment.sh` | Runs any of the above as a one-off ECS task on `georag-migrate`. |

```bash
bash ops/rehearsal/run_against_deployment.sh seed
bash ops/rehearsal/run_against_deployment.sh verify
bash ops/rehearsal/run_against_deployment.sh teardown
```

## What the verification asserts

0. The deployed `silver.pg_collars_by_project` is the **post-fence**
   definition, not a pre-fix one that would answer happily without a
   `workspace_id`.
1. The two tenants are co-located (above).
2. Positive controls — each tenant can see its own tile. Catches a fence that
   over-filters, which is just as broken and much quieter.
3. Cross-tenant denial in **both** directions. pgTAP test 74 covers one.
4. Missing and malformed `workspace_id` RAISE rather than returning a blank
   tile, which on a map is indistinguishable from "no data here".

Section 3 separates two outcomes the fence can produce, because they are not
equally bad and reporting them identically would misinform:

* **FENCE BYPASS** — a non-NULL but zero-byte tile. The `silver.projects`
  guard is gone; the collar-level predicate still withheld every feature, so
  nothing escaped.
* **DISCLOSURE** — a tile with bytes in it. Another tenant's features
  actually crossed the boundary.

## Verified how

Against a real PostgreSQL 16 + PostGIS instance, loading the actual function
body extracted from the migration, with a mutation matrix:

| mutation | result |
|---|---|
| none (healthy) | exit 0, VERIFIED |
| `p.workspace_id` guard removed | FENCE BYPASS, 0 bytes |
| both guards removed | DISCLOSURE, 445 bytes of the other tenant's features |
| missing-`workspace_id` RAISE removed | caught by section 4 |
| tenants separated + both guards removed | section 1 refuses to pass |

That last row is the point of the whole design.

## Scope

Rehearsal tooling, not part of the deploy path. Nothing in CD calls it. It
writes to a live database and `teardown_multitenant_corpus.sql` is the way
back out — read it before pointing this at anything holding real tenant data.
