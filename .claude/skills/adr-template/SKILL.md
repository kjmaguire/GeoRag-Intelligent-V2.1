---
name: adr-template
description: Architecture Decision Record (ADR) authoring template matching the GeoRAG ADR-0001 style. Use when proposing or documenting an architectural change, license/dependency swap, infrastructure replacement, or any structural decision that supersedes guidance in georag-architecture.html. Triggers on conversation mentions of "ADR", "architecture decision", "decision record", "supersedes", "deciders", or when a decision is non-obvious + worth a written rationale.
metadata:
  origin: GeoRAG project — modeled after docs/adr/0001-seaweedfs-replaces-minio.md (the existing canonical ADR).
  authoritative-sources:
    - docs/adr/0001-seaweedfs-replaces-minio.md (canonical ADR style)
    - CLAUDE.md "When you're stuck → Cross-cutting concern → senior-reviewer"
  scope: Architectural / infrastructure / dependency-level decisions. Not for implementation tickets, not for routine refactors, not for code-review discussions.
---

# GeoRAG ADR Template

ADRs document **non-obvious, hard-to-reverse decisions** with a written rationale that survives team turnover. They are NOT for routine implementation choices. The bar is: would the next maintainer six months from now ask "why did we do it this way?" — if yes, write an ADR.

> **Existing ADR to model after:** `docs/adr/0001-seaweedfs-replaces-minio.md`
> **Directory:** `docs/adr/` (singular, not plural — preserved for consistency with existing ADR-0001).
> **Naming:** `NNNN-<short-kebab-name>.md` where `NNNN` is the next free sequence number, zero-padded to 4 digits.

## When to write an ADR

Write an ADR when the decision:
- **Supersedes** existing architecture-doc guidance (`georag-architecture.html` is the source of truth — ADRs explicitly amend it)
- **Locks in** a license, vendor, or major dependency (object store, LLM serving, vector DB, etc.)
- **Is hard to reverse** (data migrations, env-var rename cascades, schema decisions affecting §04e)
- **Has a non-obvious losing alternative** that future maintainers will be tempted to revisit

Do NOT write an ADR for:
- Implementation choices fully covered by the architecture doc
- Code-review style debates
- Routine library-version bumps (use commit messages)
- Anything where the rationale fits in a comment block

## Structure (canonical, matching ADR-0001)

```markdown
# ADR NNNN: <Imperative one-line decision statement>

- **Date**: YYYY-MM-DD
- **Status**: Proposed | Accepted | Superseded by ADR-NNNN | Deprecated
- **Deciders**: <name(s) with role; SME for domain decisions>
- **Supersedes**: <reference to architecture doc section, prior ADR, or "(none)">

## Context

What problem are we solving? What facts forced the re-evaluation? Be specific
about dates, versions, license shifts, upstream events. The reader should
understand why this decision was necessary, not just what it is.

## Options considered

| Option | License | Effort | Outcome |
|---|---|---|---|
| A. <name> | <license> | <Low/Medium/High> | <Why rejected> |
| B. <name> | <license> | <effort> | <Why rejected> |
| C. **<chosen>** | **<license>** ✅ | <effort> | Chosen — see Decision below. |

(Adjust columns to fit. License + Effort + Outcome is the GeoRAG default;
add columns like "Maintenance" / "Performance" / "Compliance" when relevant.)

## Decision

State the decision in one sentence with the concrete artifact identifier
(image tag, package version, commit hash). Then break into:

### What stays the same

Bullet list of things callers / operators / configs DO NOT need to change.
This is reassurance for the reader: most of the system continues unaffected.

### What changed

Bullet list of the specific edits — image tags, port numbers, env var
defaults, file paths. Be precise enough that someone reading this 6 months
later can recreate the diff.

## Migration mechanics (for future reference)

Numbered steps to execute the change, ordered so each step is reversible
until the final commit point. Note explicitly which step crosses the
"point of no return" (volume rm, data deletion, etc.).

Example shape:
1. Snapshot existing state to <location>.
2. Stop services X, Y, Z.
3. Edit compose / config (still reversible — restart restores prior state).
4. Cut over (point of no return — snapshot is the only rollback now).
5. Verify (commands + expected outputs).
6. Retain snapshot for N days post-cutover.

## Gotchas hit during the migration (worth knowing for next time)

Numbered list of surprises encountered. Future implementers reading this
shouldn't re-hit the same trap. Cover:
- Default behaviors that were wrong for our use case
- Networking / IPv4 / IPv6 / hostname resolution issues
- File permission / executable-bit / Windows-host quirks
- Naming collisions intentionally preserved (compose service alias kept,
  even though the underlying tech changed — explain why renaming was rejected)

## Consequences

### Positive

- License compliance / upstream maintenance / footprint / new capabilities
  unlocked. One bullet per discrete benefit.

### Negative

- Single-maintainer risk / missing feature parity / different operational
  model / one-time data migration risk. Quantify when possible (bus-factor,
  feature gap, downtime window).

## Verification (this commit)

Concrete checks that prove the change landed cleanly:
- All N pre-migration objects/rows visible in the new system.
- Roundtrip test from each consuming service.
- All M stack services healthy after cutover.

## Follow-ups (NOT part of this ADR; tracked separately)

- Decisions explicitly deferred. Each follow-up ends with a trigger
  condition: "after Q3", "before any production deployment that handles
  PII", "if cluster grows past single-host", etc.
```

## Style rules

- **Decision title is imperative**: "SeaweedFS replaces MinIO", not "Choosing an object store"
- **Date the decision when it lands** (PR merge), not when first drafted
- **Status transitions**: Proposed (PR open) → Accepted (PR merged) → Superseded by ADR-NNNN (when a later ADR replaces it). Don't delete superseded ADRs; mark and link.
- **Quote actual artifacts**: image digests, package versions, file paths. Vague rationales rot.
- **Don't repeat the architecture doc**. Link to the section being superseded; don't restate it.
- **Acknowledge the loser fairly.** A future maintainer might re-evaluate; help them by stating the rejected option's strengths, not just its weaknesses.

## When to invoke `senior-reviewer`

For ADRs covering Module-level architecture (the kind that affect §04, §04i, §08, §35), invoke the senior-reviewer agent for a checkpoint review **before** marking Status: Accepted. Per CLAUDE.md the senior-reviewer is the milestone-gate reviewer; ADRs that change architectural posture qualify as milestone gates.

## File creation

```bash
# Find next ADR number:
ls docs/adr/ | tail -1
# e.g. 0003-foo.md → next is 0004

# Create:
touch docs/adr/0004-<short-kebab-name>.md

# Open and start with the template above. Status: Proposed.
```

## Pre-merge checklist

- [ ] Filename matches `NNNN-<kebab-name>.md` with the next free number
- [ ] Status reflects current state (Proposed during PR review, flip to Accepted on merge)
- [ ] All four sections present: Context, Options considered, Decision, Consequences
- [ ] At least one row in the Options table marked as the chosen option
- [ ] Verification section lists concrete commands + expected results
- [ ] Cross-references to architecture doc sections / prior ADRs use exact §-numbers and filenames
- [ ] Senior-reviewer checkpoint complete (for module-level architectural decisions)
