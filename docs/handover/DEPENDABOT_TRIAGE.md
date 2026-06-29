# Dependabot Triage — post version-sweep (2026-06-26)

Companion to `docs/handover/PR14_FOLLOWUPS.md`. This is a **manifest-derived**
triage: it predicts what the live Dependabot alert list (visible only with
`gh auth`) should look like once the `pr/14-version-audit-updates` branch
merges to `main`, and where residual review effort belongs per ecosystem.

> Why no live numbers: the GitHub Dependabot/Security-Advisory API needs an
> authenticated `gh` session, which this autonomous run does not have. Cross-
> reference the predictions below against `gh api /repos/{owner}/{repo}/dependabot/alerts`
> after merge.

## 1. Alerts that should AUTO-CLOSE on the next rescan after merge

Dependabot resolves an advisory the moment the resolved version in the lockfile
crosses the patched floor. The sweep raised these floors, so each corresponding
alert should flip to **Closed (auto-dismissed: a fix was committed)** within a
rescan cycle of the merge:

| Package | Ecosystem | Advisory | Floor raised to | Source commit |
|---|---|---|---|---|
| python-multipart | pip | GHSA-pp6c-gr5w-3c5g + 2026-53537/53540 | `>=0.0.31` (resolves 0.0.32) | batch-1 `3f531a6` |
| weasyprint | pip | CVE-2026-49452 | `<70.0` (resolves 69.0) | batch-1 `3f531a6` |
| urllib3 | pip | GHSA-mf9v-mfxr-j63j | `>=2.7.0` | (already pinned, re-verified) |
| authlib | pip | GHSA-r95x-qfjj-fjj2 | `>=1.6.12` | (already pinned, re-verified) |
| trivy-action | github-actions | `@master` tag-mutation risk | `@v0.36.0` | batch-2 `9a70156` |
| cosign-installer | github-actions | v3 EOL | `@v4` + cosign v3.1.1 | batch-2 `9a70156` |
| (transitive GH action bumps) | github-actions | various | current majors | batch-2 `9a70156` |
| alpine (backup-agent) | docker | EOL 3.20 | `3.22` | batch-1 `3f531a6` |
| postgres (langfuse-init) | docker | unpinned `:18-alpine` | `18.4` SHA-pinned | batch-1 `3f531a6` |
| redis (staging overlay) | docker | 8.6.2 known-vuln | `8.6.4` | batch-3 `3d72814` |
| curl (sidecar) | docker | 8.10.1 CVEs | `8.21.0` | batch-1 `3f531a6` |
| mc (minio client) | docker | Go stdlib CVEs | 2025-08-13 | batch-3 `3d72814` |

**Predicted auto-close count: ~12** (matches the "~12 auto-close" estimate from
the audit). Confirm post-merge; if any of these stay Open, the lockfile didn't
actually pick up the floor (check `uv.lock` / `package-lock.json` /
`composer.lock` resolved versions).

## 2. Config gaps in `.github/dependabot.yml` (actionable)

The config is healthy (all 5 ecosystems covered, grouped, security-PRs
immediate). Two improvements worth making when convenient:

1. **pip ecosystem vs uv.** The `pip` ecosystem at `/src/fastapi` reads
   `pyproject.toml` (PEP 621) but does **not** resolve `uv.lock`. Dependabot
   shipped a native **`uv`** ecosystem in 2025 — switching
   `package-ecosystem: "pip"` → `"uv"` makes version-update PRs lockfile-aware
   and stops the drift between the pin floors and what `uv.lock` actually
   resolves. Low-risk config change; do it in a standalone PR so the first
   Dependabot uv-run is reviewable in isolation.

2. **Docker grouping.** The `docker` ecosystem has no `groups:` block, so each
   sidecar image bump opens its own PR (monthly cap 5). Adding a
   `patch-updates` group (mirroring the other ecosystems) collapses routine
   base-image patch bumps into one PR. Cosmetic but cuts noise.

Neither gap is a security hole — they only affect update-PR ergonomics.

## 3. Residual review (per ecosystem) — NOT auto-closing

These are the version-update PRs Dependabot will keep cutting; each needs a
human decision because they're either majors or have deliberate caps:

- **npm** — `@inertiajs/react`, `react-plotly.js`, `concurrently` majors are
  handled in this same branch (validated). After merge, the `react` group will
  quiesce. Watch for the next **vite** / **@vitejs** major (currently on 8.x).
- **composer** — the `laravel` group tracks `laravel/*` + `inertiajs/*`; Laravel
  13 patch line is stable. No security debt; let the weekly group PR ride.
- **pip** — `langchain*`/`langgraph*` group is the active one; these move fast.
  Keep the CVE-floor pins (python-multipart, urllib3, authlib, weasyprint) as
  explicit `>=` floors even after uv-ecosystem switch — they document *why* the
  floor exists, which a bare lock entry does not.
- **github-actions** — monthly cadence; the sweep already pulled everything to
  current majors, so the next month should be quiet. `setup-uv` is on **v7**
  (v8 does NOT exist — do not let a hallucinated bump through; verified
  2026-06-24 via `git ls-remote`).
- **docker** — monthly; the SHA-pins mean Dependabot proposes digest updates,
  which are the safe kind. Review the tag delta, not just the digest.

## 4. One-command post-merge verification (run with gh auth)

```bash
# How many alerts remain open, by ecosystem + severity:
gh api -X GET /repos/{owner}/{repo}/dependabot/alerts \
  -f state=open --paginate \
  | jq -r '.[] | [.security_vulnerability.package.ecosystem,
                  .security_advisory.severity,
                  .security_vulnerability.package.name] | @tsv' \
  | sort | uniq -c | sort -rn
```

Expectation right after merge + rescan: the ~12 rows in §1 gone; whatever
remains is genuinely new advisory traffic since the sweep, triaged per §3.

---
*Generated by the autonomous overnight run, 2026-06-26. Manifest-derived —
validate against live alerts post-merge.*
