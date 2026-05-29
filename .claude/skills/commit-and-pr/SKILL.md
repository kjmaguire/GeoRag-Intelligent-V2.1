---
name: commit-and-pr
description: Conventional Commits convention plus GeoRAG's PR description template. Use when authoring git commits or pull request bodies, when squashing/rebasing, or when the working tree has staged changes ready for a commit message. Triggers on git commit, git rebase, gh pr create, or "open a PR" / "write the commit message" requests.
metadata:
  origin: GeoRAG project — derived from CLAUDE.md "Commit convention" section + the existing project's commit history style.
  authoritative-sources:
    - CLAUDE.md "Commit convention" section (canonical prefix list + section-reference rule)
    - https://www.conventionalcommits.org/ (upstream Conventional Commits spec)
  scope: Git commit messages and pull request descriptions for the GeoRAG repo. Does not cover branch naming or merge strategy (project uses squash-merge by default).
---

# GeoRAG Commit + PR Discipline

CLAUDE.md sets the rule: **conventional commits with section references** (e.g. `feat(ingestion): implement CRS detection per Section 04b`). This skill operationalizes that — what prefixes to use, when, and what a PR body should contain.

## Conventional Commits (canonical prefixes)

```
<type>(<scope>): <imperative subject>

<body — wrapped at 72 cols>

<footer — references>
```

| Type | When |
|---|---|
| **feat** | New feature visible to users / API consumers |
| **fix** | Bug fix |
| **refactor** | Code reorganized, **no behavior change** |
| **docs** | Documentation only (`docs/**`, `README.md`, `CLAUDE.md`, ADRs) |
| **test** | Test code only (no production changes) |
| **chore** | Tooling, deps, config, CI — anything not in the above |

Scope is the affected subsystem: `ingestion`, `agent`, `martin`, `vllm`, `octane`, `horizon`, `dagster`, `silver`, `pdf`, etc. Match the directory or domain you actually touched.

## GeoRAG-specific rules (from CLAUDE.md)

1. **Section references for spec-anchored work.** If the commit relates to a specific section of the architecture doc, name it:
   `feat(ingestion): implement CRS detection per Section 04b`
   `fix(agent): hallucination prevention layer 3 numerical verification per Section 04i`
2. **Imperative subject under 72 chars.** "Add", "Fix", "Refactor", "Remove" — never "Added", "Fixing".
3. **Body explains *why*, not *what*.** The diff explains what changed. The body answers why the change was necessary.
4. **Reference architecture-doc sections in the footer**, not the subject (subject stays short):
   ```
   feat(silver): add MVT function for mineral occurrences

   Martin needs a server-side MVT generator for the new
   public_geoscience.mineral_occurrences table — client-side rasterization
   on 50K+ features kills frame rate at zoom < 8.

   Refs: §04e (mineral_occurrences schema), §07c (Martin tile sources)
   ```

## Before staging — pre-flight checks

When the user asks for a commit, **verify the staged changes match the message scope** before drafting:

```bash
git status
git diff --cached
git log --oneline -5            # match prevailing prefix style
```

If staged hunks span multiple types/scopes (`feat` + `chore` + `docs`), recommend splitting into separate commits. A `feat` commit shouldn't carry an unrelated dependency bump.

## Subject-line style guide

| Good | Why |
|---|---|
| `feat(silver): add MVT function for mineral occurrences` | Imperative, scoped, under 72 chars, signals user-visible feature |
| `fix(martin): grant SELECT on silver.* to martin_ro role` | Specific fix, names exact artifact |
| `refactor(orchestrator): extract _resolve_local_llm_fallback_target` | No behavior change, names the extraction target |
| `chore(deps): bump vllm-openai image to v0.20.2` | Routine bump, scoped to deps |
| `docs(adr): add 0002 ollama→vllm cutover` | Documentation-only |

| Bad | Why |
|---|---|
| `Updated stuff` | No type, no scope, no specificity |
| `feat: many improvements to ingestion` | Vague body in subject; split |
| `Fixed bug` | Past tense, no scope, no detail |
| `feat(ingestion): added CRS detection (per Section 04b)` | Past tense; parenthetical reference belongs in body/footer |

## Commit body — what goes in

Body is optional for trivial commits (typo fix, dep bump). For anything substantive, the body answers:

1. **Why was this necessary?** Trigger condition, bug symptom, requirement that surfaced.
2. **What did we consider and reject?** When non-obvious — saves the next maintainer's review cycle.
3. **What's the blast radius?** Files / services / runtime behaviors affected.
4. **Verification?** How was it confirmed working — link to test names, smoke commands, manual probes.

### Body template

```
<one-paragraph "why" — what triggered the change>

<one-paragraph "what" — at the architectural level, not the diff level>

Verification:
  - <test name or command 1>
  - <smoke command 2>

Refs: §<section>, ADR-<NNNN>, #<issue>
```

## Pull request body template

PRs squash-merge into the trunk, so the PR body becomes the canonical commit message. Use this shape:

```markdown
## Summary

- <bullet 1: what shipped, in user-visible terms>
- <bullet 2: scope or subsystem affected>
- <bullet 3: any breaking changes or migrations needed>

## Rationale

<2–4 sentences on why now. What broke or what need surfaced. Link to
the issue / ADR / kickoff-doc step that motivated this work.>

## Changes

- <File or subsystem 1: one-line summary of the edit>
- <File or subsystem 2: …>
- <Migrations: list each new migration filename>

## Verification

- [ ] `vendor/bin/pint --dirty --format agent` clean
- [ ] `php artisan test --compact <relevant filter>` green
- [ ] `php artisan migrate` + `php artisan migrate:rollback` round-trip clean (if migrations)
- [ ] Manual smoke: <specific command(s) from the relevant runbook>
- [ ] Senior-reviewer checkpoint complete (for module-level architectural changes)

## Cross-references

- Architecture doc: §<section(s)>
- ADR: ADR-<NNNN> (if applicable)
- Master plan: <step or registry entry>
- Issue: #<number>
```

Adjust the verification checklist to the actual change — don't include irrelevant items (no migrations? skip the migrate line).

## Commit-time checklist

Before issuing `git commit -m "..."`:

```bash
# 1. Inspect what's staged
git diff --cached --stat
git diff --cached            # actually read it

# 2. Pint on touched PHP files (per CLAUDE.md):
vendor/bin/pint --dirty --format agent

# 3. Run focused tests for the change:
php artisan test --compact --filter=<relevant-test-filter>

# 4. Subject + body composed per the rules above

# 5. Commit
git commit                   # opens editor for full body — preferred
# OR for trivial:
git commit -m "<type>(<scope>): <subject>"
```

## Anti-patterns to avoid

| Anti-pattern | Why it's bad | Fix |
|---|---|---|
| Mixing `feat` + unrelated `chore` in one commit | Hard to revert one half cleanly | Split via `git reset` + stage selectively |
| Subject "Update X" with no type | Loses the type taxonomy that drives changelog/tooling | Pick a type honestly |
| `fix:` for a refactor with no bug | Inflates fix-rate metrics, misleads CHANGELOG | Use `refactor:` |
| Body describes the diff line-by-line | Diff is right there; body should explain *why* | Rewrite body to answer "what triggered this" |
| Section reference in subject `(per §04b)` | Eats subject character budget for no readability win | Move to footer `Refs: §04b` |
| Co-Authored-By trailers without explicit user request | Project policy: only add when explicitly asked | Don't add unless instructed |

## When the user says "commit this"

1. Inspect `git status` + `git diff --cached`
2. Identify type + scope from the actual change
3. Draft subject (under 72 chars, imperative, scoped)
4. Draft body if substantive (why + verification + refs)
5. Stage any pint-formatted files
6. Issue the commit
7. Run `git status` after to confirm clean
