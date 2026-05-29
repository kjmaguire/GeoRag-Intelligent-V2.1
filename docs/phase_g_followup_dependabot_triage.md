# Phase G follow-up — Dependabot triage (partial)

**Status:** ✅ npm side closed (3 → 0). ✅ composer side clean (0 → 0).
⚠️ Python side **needs user-authenticated `gh` to inspect** —
non-interactive pip-audit isn't installable from inside the FastAPI
container's pip configuration.

## What landed

### npm side — 3 vulnerabilities → 0

| Package | Severity | Issue | Fix |
|---|---|---|---|
| `follow-redirects` | moderate | (transitive) | bumped via `npm audit fix` |
| `postcss` | moderate | XSS via unescaped `</style>` in CSS stringify output (GHSA-qx2v-qp2m-jg93) | bumped via `npm audit fix` |
| `axios` (devDependency) | high | XSRF token cross-origin leakage via prototype pollution in `withXSRFToken` boolean coercion (GHSA-xx6v-rp6x-q39c) + credential injection in HTTP adapter (GHSA-q8qp-cvcw-x6jj) | pin updated `>=1.11.0 <=1.14.0` → `>=1.16.1 <2.0.0`; `npm audit fix --force` no longer required |

Final `npm audit` (production + dev): **found 0 vulnerabilities**.

### composer (PHP) side — already clean

`composer audit --format=json` returned `{"advisories": [], "abandoned": []}`.

## What's left

The push the other day reported 29 Dependabot alerts on the GitHub
side (12 high / 16 moderate / 1 low). With npm closing 3 and composer
contributing 0, **roughly 26 alerts are presumably Python-side**
(pyproject.toml / requirements). I cannot scan these tonight because:

1. `pip-audit` install fails inside the FastAPI container — read-only
   filesystem on `site-packages` and the wheel resolution times out
   against PyPI in the test environment.
2. `gh api repos/.../dependabot/alerts` requires `GH_TOKEN` or
   `gh auth login` — interactive only.

### Operator steps to close the Python side

1. **From a host shell with `gh` authenticated**, run:

   ```
   gh api repos/kjmaguire/GeoRAG-Intelligent-V2.0/dependabot/alerts \
     --paginate -q '.[] | select(.state=="open") |
       {n:.number, sev:.security_advisory.severity,
        pkg:.dependency.package.name, eco:.dependency.package.ecosystem,
        fix:.security_vulnerability.first_patched_version.identifier}'
   ```

2. **Filter** the output for `eco == "pip"` and bump the listed
   packages in `pyproject.toml` (or wherever pinned). The fix
   identifier is the minimum safe version per advisory.
3. **Rebuild** the FastAPI image to refresh the wheel set:
   ```
   docker compose build fastapi --no-cache
   docker compose up -d fastapi
   ```
4. **Verify** with `pytest tests/ -q` that no test regresses after
   the bumps.
5. If a bump cascades into a broken transitive dep, prefer a
   constraint pin (`package>=X.Y,<X.Z`) over `--force` upgrades.

### Files touched (this pass)

* `package.json` — axios pin bumped to `>=1.16.1 <2.0.0`
* `package-lock.json` — postcss + follow-redirects + axios resolved
  versions updated

The lockfile diff is +43 / −13 lines.

## Why this doc exists

The Dependabot push warning is non-blocking and easy to lose. This
doc gives Kyle (or a future operator tick) a 5-minute procedure to
close the remaining Python-side alerts without re-discovering the
environmental constraints from scratch.
