# Audit Invariants

This document is the **index** of every PR-time invariant a prior
audit produced. Each entry is one file that pins one bug class. If
you're here because CI failed, scan for your test name — the entry
explains what the guard catches and how to either fix the bug or
update the guard.

**The rule:** never delete an audit invariant without an explicit
acknowledgement in the PR description. The invariant exists because
the bug actually shipped before — removing it without context invites
the same bug back.

When you add a new audit guard (file-content test, regression test,
pre-commit hook), add it here too. The
`.github/workflows/audit-invariants.yml` CI workflow wires the
file-content checks in; this doc is the human-readable index.

---

## Why this layer exists

The pattern that led to this:

> Two `audit-and-fix.md` remediation passes happened in 30 days
> (2026-06-02, 2026-06-03). Both surfaced the same shape of findings —
> workspace-resolution fallbacks, RLS fail-open policies, stale CHECK
> enums, dead settings, comment/code drift. The audit pattern was
> CATCHING bugs after merge, not preventing them.

Each entry below is a guard that flipped one specific bug from
"catchable in audit" to "catchable in PR review." A PR that
introduces the bug fails this workflow before review. A PR that
*intentionally* changes the invariant must update the guard and
explain why.

---

## Tenancy + workspace isolation

| Guard | File | What it catches |
|---|---|---|
| Workspace-user pivot exists + has the contract A1 declared | [`WorkspaceUserMembershipTest.php`](../tests/Feature/Tenancy/WorkspaceUserMembershipTest.php) | Anyone removing the `workspace_user` table, the User model's `defaultWorkspaceId()` helper, or the Inertia auth share that surfaces `current_workspace_id` to the frontend |
| Rate limiters key on workspace_id, not user_id | [`WorkspaceRateLimitsTest.php`](../tests/Feature/Tenancy/WorkspaceRateLimitsTest.php) | A "let's simplify the throttle" refactor that switches `'uploads'` / `'charts'` back to per-user keying — would let one operator drain the team's compute budget |
| ProjectController + OnboardingController use defaultWorkspaceId() | [`ProjectCreationWorkspaceResolutionTest.php`](../tests/Feature/Tenancy/ProjectCreationWorkspaceResolutionTest.php) | Reverting to `$user->workspace_id` (legacy column that never existed) — the `?? hardcoded-default` fallback then fires unconditionally and every new project lands in the seeded default tenant |
| Workspace channel auth requires a real project in that workspace | [`WorkspaceActivityChannelAuthTest.php`](../tests/Feature/Tenancy/WorkspaceActivityChannelAuthTest.php) | Cross-tenant Reverb subscribe — any logged-in user could subscribe to any workspace's activity channel until Theme F closed this |
| No legacy `georag.workspace_id` GUC writes in PHP | [`NoLegacyGucSetConfigInPhpTest.php`](../tests/Feature/Tenancy/NoLegacyGucSetConfigInPhpTest.php) | Re-introduction of `set_config('georag.workspace_id', ...)` — the canonical RLS uses `app.workspace_id` and the legacy GUC name is a fail-open variant |
| WorkspaceContext is the only resolution path in agent code | [`test_workspace_context.py`](../src/fastapi/tests/test_workspace_context.py) | A new `getattr(state.deps, "workspace_id", None) or DEFAULT` site inside `app/agent/` — Phase-2 cutover will swap WorkspaceContext from observe-only to hard-raise, the test pins that callers can graduate cleanly |
| Centralised LEGACY_DEFAULT_TENANT_UUID across 18 production files | [`test_workspace_context_b4_centralisation.py`](../src/fastapi/tests/test_workspace_context_b4_centralisation.py) | Copy-pasting the literal `"a0000000-0000-0000-0000-000000000001"` into a new file. The constant lives in ONE place so the Phase-2 cutover is a single edit, not a 30-site sweep |

## Postgres safety + RLS

| Guard | File | What it catches |
|---|---|---|
| Martin tile-server role is read-only (no `georag_write`) | [`MartinRoleReadOnlyTest.php`](../tests/Feature/Tenancy/MartinRoleReadOnlyTest.php) | A "give martin_ro write access for convenience" PR — Martin compromise would re-become silver compromise. Pin also covers the `INHERIT` flag the audit normalized |
| Shadow-trigger handlers bind app.workspace_id by parameter, not f-string | [`test_shadow_trigger_observability.py`](../src/fastapi/tests/test_shadow_trigger_observability.py) | A return to f-string `SET LOCAL` (injection risk) in the three shadow-trigger handlers — Theme G fix |
| Connection-scope helper retired no-op pattern | [`test_acquire_scoped.py`](../src/fastapi/tests/test_acquire_scoped.py) | Restoring the wrong `acquire_scoped()` form that prior incidents pinned. Includes a sub-test that no production file calls the legacy GUC name |

## Observability + lifecycle

| Guard | File | What it catches |
|---|---|---|
| `silver.archive_ingest_runs` parent table + lifecycle helpers + on_failure_task | [`ArchiveIngestRunsMigrationTest.php`](../tests/Feature/Tenancy/ArchiveIngestRunsMigrationTest.php), [`test_ingest_zip_archive_observability.py`](../src/fastapi/tests/test_ingest_zip_archive_observability.py) | A regression to silent ZIP-upload failures. Together these pin: migration shape, RLS, status CHECK, FK to ingest_progress, archive_lifecycle context manager, on_failure_task registration |
| Source-trust boost wiring + shadow-vs-live branch | [`test_source_trust_boost_wiring.py`](../src/fastapi/tests/test_source_trust_boost_wiring.py) | Removing the wire seam or skipping the SHADOW_MODE check. Without this, `boost_by_trust` becomes dead code again, or the "shadow rollout discipline" gets short-circuited |

## Frontend hygiene

| Guard | File | What it catches |
|---|---|---|
| Legacy 1,585-line `Pages/Chat.tsx` stays deleted | [`LegacyChatPageDeletedTest.php`](../tests/Feature/Frontend/LegacyChatPageDeletedTest.php) | "Quick chat tweak" PRs that re-create `Pages/Chat.tsx` from a scaffold because the contributor expected the conventional Inertia layout instead of `Foundry/Chat.tsx` |

## Settings + payload shapes

| Guard | File | What it catches |
|---|---|---|
| Dead-setting inventory matches documented count | [`test_dead_settings_tagged.py`](../src/fastapi/tests/test_dead_settings_tagged.py) | Silent additions to / removals from the dead-setting list. Forces explicit acknowledgement of wire-or-delete decisions |
| vLLM payload shape (no Ollama leftovers, top-level sampling params, JSON via `response_format`) | [`test_vllm_payload_shape.py`](../src/fastapi/tests/test_vllm_payload_shape.py) | Refactors that re-introduce Ollama's `options` block, `format: "json"`, or `think` field on the vLLM branch. vLLM accepts unknown fields silently in some builds + rejects them in others; sending them is wrong either way |

---

## Pre-commit hook subset

The fastest checks above are also wired into `.pre-commit-config.yaml`
so they fire locally (and on push, if you `pre-commit install`).
Specifically:

- `no-legacy-ollama` — same bug class as `test_vllm_payload_shape`, but
  at the PHP / config layer
- `no-legacy-dashboard` — same class as `LegacyChatPageDeletedTest` but
  for the pre-Foundry Dashboard tree
- `system-prompt-version-bump` — prevents silent prompt drift that
  bypasses Anthropic cache invalidation
- `fastapi-pydantic-freshness` — catches stale container vs file mtime,
  which was an earlier "smoke + flow JWT verifies fail silently" bug
- `pyproject-covers-imports` / `pyproject-covers-imports-dagster` —
  catches new top-level imports that aren't declared in pyproject

Plus the new audit-invariants additions (see workflow + this file):

- `audit-invariants-php` — runs the PHP file-content tenancy guards
- `audit-invariants-python` — runs the Python file-content guards

---

## How to add a new invariant

1. Write the regression test. Pattern:
   - File-content / import-time assertions so it runs without a live DB / LLM / Qdrant
   - Each assertion message tells the contributor what specifically to do if it fails
   - One file = one bug class so failures point at one thing
2. Add the test path to `.github/workflows/audit-invariants.yml`
3. Add a row to the relevant table above with a one-line "what it catches"
4. If it's fast enough for pre-commit, add a hook entry to
   `.pre-commit-config.yaml` too — local catch is faster than CI catch

## How to retire an invariant

Don't remove the entry casually — the bug class shipped before, and
removing the guard without explanation invites it back.

When you genuinely retire one (the bug class became impossible, the
file moved, etc.):

1. Delete the test file
2. Delete the row from this doc
3. Delete the entry from `.github/workflows/audit-invariants.yml`
4. In the PR description, name the original bug class and explain
   why it can no longer happen (eg. "WorkspaceContext is now
   constructed only at the auth boundary; the legacy `getattr or
   DEFAULT` pattern is a type error")
