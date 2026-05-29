# Dependabot status — 2026-05-17

GitHub Dependabot flags **11 vulnerabilities** on `kjmaguire/GeoRAG-Intelligent-V2.0`'s
default branch (7 high, 4 moderate) as of this commit.

## What I checked

| Audit | Result |
|---|---|
| `npm audit` (in `node:22-alpine`) | `0 vulnerabilities` across 699 packages |
| `composer audit` (in `composer:2`) | `No security vulnerability advisories found.` |
| `pip-audit` on FastAPI freeze (`pip freeze` → 329 deps) | Did not complete cleanly via the docker bind-mount path (parser error). Will re-check with `gh auth login` + `gh api dependabot/alerts`. |

## Most likely explanation

The 11 alerts are **inventory drift** — Dependabot retains historical
alerts even after the underlying package is upgraded or removed. The
project has churned heavily (multiple framework upgrades, ADR-0001
swap from MinIO → SeaweedFS, ADR-0002 swap from RAGFlow → §04p PDF
stack, Activepieces → Kestra swap) — each of those would have left
stale dep entries Dependabot still references.

## Action required

To definitively close out these 11 alerts:

1. Run **on a machine with `gh` authenticated** (a personal access
   token with `security_events` scope works):

   ```bash
   gh api -X GET /repos/kjmaguire/GeoRAG-Intelligent-V2.0/dependabot/alerts \
       --jq '.[] | select(.state=="open") | {pkg: .dependency.package.name, eco: .dependency.package.ecosystem, sev: .security_advisory.severity, summary: .security_advisory.summary}'
   ```

2. For each alert returned:
   - **If the package is in current `package.json` / `composer.json` /
     `pyproject.toml`:** update to a fixed version.
   - **If the package is no longer referenced** (e.g. activepieces deps,
     ragflow deps, minio deps): dismiss as `not-vulnerable-code` via the
     GitHub UI.
   - **If it's a transitive dep with no fixed upstream:** dismiss as
     `inaccurate` or `won't-fix` with the rationale.

3. After closeout, the Dependabot push warning should disappear on the
   next `git push`.

## Why I didn't auto-resolve in this session

Without `gh auth login` configured on this build host, I can't
enumerate the alerts to know which packages they reference. The
audit tools that DON'T need auth (`npm audit`, `composer audit`)
both came back clean, which strongly suggests the alerts are stale
not actionable — but I can't prove it from here.

## Sub-task for the next operator-paired session

```bash
gh auth login --scopes "repo,security_events"
gh api /repos/kjmaguire/GeoRAG-Intelligent-V2.0/dependabot/alerts \
    --jq '[.[] | select(.state=="open")] | length'
# expect 11; then enumerate + close.
```

ETA: ~15 minutes once authenticated.
