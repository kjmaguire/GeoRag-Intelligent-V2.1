# Audit Ledger Hash Recipe

**Status:** Locked Phase 0 — any change requires an ADR plus a coordinated update to all four implementations below.
**Audience:** GeoRAG implementers, regulators / external auditors verifying chain integrity.
**Phase 0 reference:** kickoff Step 4 (this document) + Step 2 trigger (`audit.compute_audit_hash`).

---

## Why this document exists

The `audit.audit_ledger` table is the system's tamper-evident record. Every state-changing event writes one row; each row's `hash` column commits to:

- the previous row's hash (chaining),
- this row's content (actor, action, target, payload, timestamp).

Any later modification of any field — or any insertion / deletion in the middle of the chain — makes every subsequent row's stored hash diverge from the recomputed hash. The verifier walks the chain nightly and surfaces breaks.

For this to be **independently verifiable**, the recipe must be precise enough that an external auditor — given the database contents and this document — can reproduce every hash without referring to GeoRAG application code.

---

## The recipe

For each `audit.audit_ledger` row, `hash` is computed as:

```
hash = SHA-256( previous_hash_hex
              + '|' + actor_id_text
              + '|' + actor_kind
              + '|' + action_type
              + '|' + target_schema
              + '|' + target_table
              + '|' + target_id
              + '|' + payload_text
              + '|' + created_at_iso_utc )
```

Where:

| Field | Encoding |
|---|---|
| `previous_hash_hex` | hex-encoded bytes of the previous row's `hash`. Empty string `""` for the first row in a chain (NULL `previous_hash`). |
| `actor_id_text` | `actor_id` rendered as decimal text (`""` if NULL). |
| `actor_kind` | one of `user`, `system`, `agent`, `workflow`, `external`. |
| `action_type` | the canonical action name, verbatim. |
| `target_schema` / `target_table` / `target_id` | verbatim text values, `""` if NULL. |
| `payload_text` | Postgres' `jsonb::text` representation of the payload column. **This is deterministic for a given `jsonb` value** — Postgres stores keys in length-then-lex order, and serialisation is stable. |
| `created_at_iso_utc` | timestamp formatted in UTC with microsecond precision: `to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')` — e.g. `2026-05-09T22:14:33.421875Z`. |
| separator `\|` | the ASCII pipe character, exactly. |

**Chain scoping:** rows are chained per workspace. Two rows belong to the same chain iff their `workspace_id` values are equal under `IS NOT DISTINCT FROM` (so `NULL = NULL` for system-wide events). The chain order within a scope is `(created_at ASC, id ASC)`.

**Where `created_at` comes from.** The trigger assigns it: `NEW.created_at := clock_timestamp()`, taken *after* the per-workspace advisory lock is held and before the previous row is read (`2026_10_10_100100`). A caller cannot choose it, and an explicit value in the `INSERT` is overwritten. This is what keeps the chain order `(created_at ASC, id ASC)` equal to the order in which writers actually linked to each other. Taking the timestamp from the column `DEFAULT` instead was evaluated before the trigger waited for the lock, so a writer that locked later could carry an earlier timestamp, and the verifier would report a break on a chain that was never touched.

---

## Where this recipe lives in the system

The recipe is implemented in **four places** that must stay in lockstep:

| Where | What |
|---|---|
| `audit.compute_audit_hash()` PL/pgSQL trigger | Computes the hash on every INSERT. Source of truth for live writes. |
| `audit.recompute_hash(...)` SQL function | Pure-SQL mirror used by the verifier. |
| `audit.verify_hash_chain(start, end)` SQL function | Walks rows in (workspace_id, created_at, id) order, calls `recompute_hash`, returns mismatches. |
| This document | Human-readable canonical reference. |

Any change to one **must** be reflected in the other three. The smoke test (`scripts/phase0_audit_outbox_smoke.sh`) inserts a synthetic chain and runs the verifier; it fails fast if the trigger and the verifier drift apart.

---

## External verification (auditor playbook)

An external auditor with read-only Postgres access can reproduce the verifier without GeoRAG application code:

```sql
-- Verify a date range. Returns mismatched rows; empty = chain intact.
SELECT * FROM audit.verify_hash_chain(
    '2026-05-09 00:00:00+00'::timestamptz,
    '2026-05-10 00:00:00+00'::timestamptz
);
```

Or roll up via the wrapper that records the run:

```sql
SELECT audit.run_verification(
    '2026-05-09 00:00:00+00'::timestamptz,
    '2026-05-10 00:00:00+00'::timestamptz
);
-- → uuid of the verification run; final status:
SELECT status, rows_verified, broken_ids
FROM audit.audit_ledger_verification_runs
WHERE id = '<the uuid above>';
```

For a **fully independent** check (no GeoRAG functions called), the auditor can write the recipe in any language. A reference implementation in Python:

```python
import hashlib, json
from datetime import datetime

def recompute(prev_hash: bytes | None,
              actor_id: int | None,
              actor_kind: str,
              action_type: str,
              target_schema: str | None,
              target_table: str | None,
              target_id: str | None,
              payload_jsonb_text: str,
              created_at: datetime) -> bytes:
    parts = [
        prev_hash.hex() if prev_hash else '',
        str(actor_id) if actor_id is not None else '',
        actor_kind or '',
        action_type,
        target_schema or '',
        target_table or '',
        target_id or '',
        payload_jsonb_text,
        created_at.astimezone().strftime('%Y-%m-%dT%H:%M:%S.%fZ').replace('+00:00','Z'),
    ]
    return hashlib.sha256('|'.join(parts).encode('utf-8')).digest()
```

Walk the rows in `(workspace_id, created_at, id)` order, threading `prev_hash` from one row's computed hash into the next.

---

## Known limitations / Phase 11 hardening

1. **Canonical JSON.** The recipe uses Postgres' `jsonb::text` (deterministic per-value but not RFC-8785 JCS compliant). For external auditors who need RFC-8785, Phase 11 will add a `payload_jcs_text` generated column populated by a JCS implementation in PL/pgSQL. The chain will be re-anchored at that point with a `recipe_version` discriminator on `audit_ledger`.

2. **Per-workspace partition vs global chain.** Today system-wide events (workspace_id NULL) form a single global chain. If multiple GeoRAG instances ever share a database, a global chain becomes contention; Phase 11 will add a `chain_id` column to scope.

3. **Genesis row.** The first row inserted (action_type `audit_ledger.genesis`) has `previous_hash = NULL`. The verifier treats this correctly (empty-string prefix in the hash input). If the genesis row is ever deleted, every subsequent verification fails — by design.

4. ~~**Trigger UPDATE protection.**~~ **Closed 2026-10-10** (`2026_10_10_100200_make_audit_ledger_append_only`). `UPDATE` and `DELETE` are revoked from `georag_app` (and the NOLOGIN grant-holder `georag_write`), and a `BEFORE UPDATE OR DELETE` row trigger, `audit_ledger_append_only_trg`, raises for every other role, the table owner and superusers included. The only way to change a stored row is a superuser setting `session_replication_role = replica` for the transaction, which stops ordinary triggers firing; that is the break-glass for a deliberate, reviewed prune. `TRUNCATE` is not covered (the application role has never held it). The hash trigger no longer takes a row lock on the tail row, because `SELECT ... FOR UPDATE` needs the `UPDATE` privilege that the role no longer has; the advisory lock was always the serialiser.

---

## Spec deviations from kickoff

The Phase 0 kickoff specifies JCS (RFC 8785) for the canonical-JSON step. Phase 0 implements `jsonb::text` instead — adequate for in-system verification but not strictly RFC-8785. See item 1 above. Tracked for v2.4.3 doc revision and Phase 11 hardening.
