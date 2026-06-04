# ADR-0014: Two-phase workspace scoping for support-context workflows

**Status:** Proposed (2026-06-03)
**Authors:** REC#2 Phase-2 architectural decision
**Supersedes:** N/A
**Superseded by:** N/A

## Context

The REC#2 Phase-2 migration sweep collapsed 38 of 56 bespoke
`set_config('app.workspace_id', ...)` sites to the canonical
`scoped_connection` / `bind_workspace_scope` helpers. The remaining
6 production sites — 5 in `app/services/support_cockpit/` and 1 in
`app/hatchet_workflows/support_replay.py` — all follow a distinct
*two-phase* pattern that neither helper supports cleanly:

```python
async with pool.acquire() as conn:
    async with conn.transaction():
        # Phase 1: bootstrap GUC to default tenant so the ticket
        # lookup succeeds (the caller has only ticket_id, doesn't yet
        # know which tenant it belongs to)
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)",
            LEGACY_DEFAULT_TENANT_UUID,
        )
        # Phase 2: discover the ticket's real workspace
        ticket = await conn.fetchrow(
            "SELECT workspace_id, category, ... FROM ops.support_tickets WHERE ticket_id = $1",
            ticket_id,
        )
        if ticket is None:
            raise ValueError(...)
        # Phase 3: REBIND the GUC to the ticket's real workspace
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)",
            ticket["workspace_id"],
        )
        # Phase 4: subsequent reads/writes scoped to the real workspace
        ...
```

This pattern is **architecturally correct** — support workflows are
intentionally cross-tenant for the initial lookup (an ops agent
handling ticket #42 doesn't pre-know its workspace) but must scope
all subsequent operations to the discovered tenant. The current
code is correct; it's just hand-rolled in 6 places.

## Options considered

### Option A: Leave the 6 sites as-is

**Pro:** Zero risk of regression in working code.
**Con:** Three audits already flagged variants of "hand-rolled GUC"
as a recurring bug class. Six instances of `ticket["workspace_id"]`
interpolated directly into `SET LOCAL` without UUID validation =
six future opportunities for a bad row to inject SQL or open the
wrong RLS scope. The audit-invariants baseline test correctly keeps
them in the allowlist, but that's documenting the problem, not
fixing it.

### Option B: Use existing `scoped_connection` + manual rebind

The user could call `scoped_connection(pool, workspace_id=LEGACY_DEFAULT_TENANT_UUID)`
to enter the transaction, do the lookup, then call
`bind_workspace_scope(conn, workspace_id=ticket["workspace_id"])`
to rebind.

**Pro:** No new helper; reuses existing primitives.
**Con:** The two calls are still hand-coordinated. The bootstrap
workspace is mentioned explicitly at the call site, which means
`bootstrap_workspace_id(reason=...)` allowlist gets bypassed (the
metric won't see the bootstrap). And the *intent* — "lookup then
rebind" — isn't named anywhere, so a future reader sees two
unrelated-looking calls.

### Option C: New `lookup_and_rescope` helper that captures the entire pattern (CHOSEN)

```python
async with lookup_and_rescope(
    pool,
    lookup_sql="SELECT workspace_id, category, status FROM ops.support_tickets WHERE ticket_id = $1::uuid",
    lookup_args=(ticket_id,),
    site="support_cockpit.escalation_routing",
    bootstrap_reason="support_replay.bootstrap_lookup",
) as (conn, ticket):
    # ticket["category"], ticket["status"], etc. ready to use
    # GUC is bound to ticket["workspace_id"] (UUID-validated)
    await conn.execute("INSERT INTO ...", ...)
```

**Pro:**
- Encodes the entire two-phase pattern in one helper — six 15-line
  blocks collapse to six 6-line blocks
- UUID-validates the pivot value (catches malformed `ticket.workspace_id`
  before SET LOCAL interpolation — fixes a real-but-latent vulnerability)
- Routes the bootstrap through `bootstrap_workspace_id(reason=...)`
  so the `WORKSPACE_RESOLUTION_FAILURES{site="bootstrap:support_replay.bootstrap_lookup"}`
  counter fires — ops sees cross-tenant elevation rate
- Returns the looked-up row so caller doesn't redo the SELECT
- Single transaction wraps the entire pattern (matches existing semantics)
- Raises `BareConnectionError` if the lookup returns no rows, or if
  the returned `workspace_id` is missing/empty/non-UUID

**Con:**
- Adds a new helper to `app/db/` surface area (3rd primitive after
  `scoped_connection` + `bind_workspace_scope`)
- The helper is specific to "lookup-row-then-pivot" semantics —
  doesn't generalize to other two-phase patterns we haven't seen yet
- Slight learning curve for new contributors who haven't seen the
  pattern before

### Option D: Refactor the support workflows to take `workspace_id` as a required parameter

Make every support workflow signature take `workspace_id: str` as
required input. The dispatcher (Laravel Horizon job, scheduled
sweeper) does the lookup once at the top-level and threads the
workspace through.

**Pro:** Cleanest architecturally — the workflow never needs to
elevate. `scoped_connection` works as-is.
**Con:** Requires changes to every dispatcher (Laravel side +
Hatchet side) AND a schema-level guarantee that ops UI surfaces
always carry `(ticket_id, workspace_id)` together. The Laravel-side
work alone is a multi-day refactor. Premature for the cleanup goal.

## Decision

**Option C** — implement `lookup_and_rescope` in `app/db/scoped_pool.py`,
migrate the 6 production sites to use it, retire those entries from
the REC#2 Phase-2 allowlist.

## Rationale

1. **Real defect fixed:** the UUID validation on the pivot value
   catches a class of bug that no current code catches. Even if no
   bug is *currently* triggering this, the audits have shown the
   pattern is fragile.

2. **Single new primitive, focused scope:** the API surface grows
   by one async context manager. It's named for what it does. A
   new contributor reads `lookup_and_rescope(pool, lookup_sql=...,
   site=...)` and immediately understands: this is the
   look-up-then-pivot pattern.

3. **Observability win:** routing through `bootstrap_workspace_id(
   reason=...)` makes cross-tenant elevations countable. If a
   dispatcher path that *should* carry a workspace ever starts
   showing up in the counter, ops sees it.

4. **The audit-invariants baseline test shrinks 56 → 12** (the
   canonical helper + audit save/restore + 4 router files +
   archived dead code + 5 cockpit files migrated = 6 fewer hits).

## Implementation outline

1. Add `lookup_and_rescope` to `src/fastapi/app/db/scoped_pool.py`
2. Add the new bootstrap reason `support_cockpit.elevated_lookup`
   to `ALLOWED_BOOTSTRAP_REASONS` in `_workspace_input.py`
3. Migrate the 5 `support_cockpit/*.py` + 1 `support_replay.py`
   sites to use the helper. Single PR — they're all sibling code.
4. Update `tests/test_scoped_connection.py` baseline allowlist to
   remove the 6 migrated files.
5. Add `tests/test_lookup_and_rescope.py` pinning the contract:
   refusal on missing/empty/non-UUID pivot; correct GUC sequence;
   row returned matches `lookup_sql` first row.
6. Update `docs/AUDIT_INVARIANTS.md` index.

## What gets caught if this is wrong

The new helper's UUID validation throws `BareConnectionError`
synchronously. If `ticket["workspace_id"]` is corrupt the call site
fails loudly inside the transaction — the partial work rolls back,
the support workflow returns an error to its caller, the operator
sees a structured exception instead of a silent cross-tenant write.

A new contributor adding a 7th two-phase site will see the helper
in `app/db/__init__.py` and use it; the audit-invariants test
catches them if they hand-roll the pattern instead.

## What we explicitly are NOT doing

- **Not refactoring the dispatcher side** (Option D) — that's a
  multi-day effort across both Laravel and Hatchet boundaries.
  Worth doing eventually, but the helper unblocks the cleanup goal
  without requiring it.
- **Not generalizing the helper** to support arbitrary two-phase
  patterns. If a 7th distinct two-phase pattern emerges, write a
  second helper. Premature generalization is what produced
  `set_config` everywhere in the first place.
