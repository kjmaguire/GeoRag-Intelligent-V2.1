# Codex handoff, round 3 — dependency security, storage-abstraction merge, remaining polish

**Prepared:** 2026-07-29, by Claude, following up on round 2
(`docs/codex-handoff-followup-2026-07-29.md`, delivered as
[PR #97](https://github.com/kjmaguire/GeoRag-Intelligent-V2.1/pull/97),
commit `080afb8`, verified mergeable and not yet merged by Kyle).
**Intended runner:** same as rounds 1-2 — Codex 5.6 "Sol", Medium reasoning,
run to exhaust the remaining ~26% of the allotted credit budget.

**Repo / branch:** `trim/phase-a-deletions`, worktree
`C:\Users\GeoRAG\Herd\georag-trim`. Same rule as always — do not touch
`C:\Users\GeoRAG\Herd\georag` (the live stack, `georagintelligencev10`).
Base this round on PR #97's head (`codex/ocr-completion-repo-review`) if it's
still open, or on `trim/phase-a-deletions` directly if Kyle has merged #97 by
the time this runs — check first with `git log --oneline -5` on both.

**Where things stand:** every item in the original trim plan (Phases A and B,
tasks A1-A7, B1-B5) is now done and verified on this branch — Neo4j dropped,
Dagster services dropped, monitoring/vllm-vl/ofelia/Martin dropped, Kestra
mostly retired, embedding dimension fixed, worker pools merged, frontend
trimmed to the reader core, OCR swapped to Azure Document Intelligence with
tiling and confidence-based review routing (rounds 1-2). The plan itself is
essentially complete. This round is cleanup and hardening on top of a
finished trim, not more deletion.

**Test recipe** — unchanged:

```bash
MSYS_NO_PATHCONV=1 docker run --rm --network georag \
  -v "//c/Users/GeoRAG/Herd/georag-trim/src/fastapi:/app" -w /app \
  -u 33:33 -e HOME=/tmp -e XDG_CACHE_HOME=/tmp/xdg_cache -e HF_HOME=/tmp/hf_cache \
  --env-file "C:/Users/GeoRAG/Herd/georag/.env" \
  -e POSTGRES_HOST=postgresql -e POSTGRES_DIRECT_HOST=postgresql \
  georag/fastapi:latest python -m pytest -q
```

Baseline after round 2: **2508 passed, 25 skipped** (FastAPI), **43/43**
golden queries, **333 passed, 150 skipped** (Laravel), **283 passed**
(Vitest). Any new failure is a real regression.

---

## Priority order

### 1. Dependabot high-severity triage (highest priority)

GitHub's Dependabot currently reports **116 open alerts on the default
branch — 46 high, 57 medium, 13 low** — separate from and *not* caught by
the local `composer audit`/`npm audit` runs in round 2 (those were clean;
Dependabot's advisory database is broader and includes transitive pip deps
that neither audit tool checks). Full high-severity list as of this handoff:

```
npm   axios              — proxy config leaks across intercepted requests
npm   form-data          — CRLF injection via unescaped multipart names
npm   shell-quote        — quadratic-complexity DoS in parse()
npm   vite               — server.fs.deny bypass on Windows alternate paths
npm   ws                 — memory exhaustion DoS from tiny fragments
pip   Mako               — path traversal (2 CVEs, Windows backslash + double-slash URI)
pip   Pillow / pillow    — 10 separate CVEs: decompression-bomb bypasses in
                            PdfStream.decode(), BdfFontFile, GdImageFile,
                            PcfFontFile, FontFile.compile() (all skip
                            _decompression_bomb_check()); heap OOB writes in
                            Image.paste()/crop()/RankFilter/ImageCmsTransform;
                            OOB read via row stride on mmap path
pip   cryptography       — vulnerable OpenSSL bundled in wheel
pip   lxml               — XXE via default iterparse()/ETCompatXMLParser()
pip   mcp                — 3 CVEs in the MCP Python SDK (task-cancel cross-
                            client, unauthenticated HTTP session requests,
                            missing WS Host/Origin validation)
pip   pyasn1             — 2 CVEs, quadratic-complexity / resource-exhaustion DoS
pip   pyjwt              — HMAC/RSA key-confusion (HS256 forgery from a public key)
pip   pypdf              — 2 CVEs, infinite loop on unterminated inline images
pip   python-multipart   — 2 CVEs, unbounded header / quadratic querystring DoS
pip   soupsieve          — 2 CVEs, ReDoS + memory exhaustion in selector parsing
pip   starlette          — 2 CVEs, SSRF/NTLM-theft via UNC paths in StaticFiles
                            (Windows only) + silently-ignored form() limits
pip   transformers       — 2 CVEs, RCE during model init / LightGlue loading path
pip   urllib3            — 2 CVEs, decompression-bomb bypass + cross-origin
                            header leak on proxied redirects
```

Work this list top to bottom by relevance to this codebase, not raw CVE
count:

- **Pillow is the standout.** This branch's own OCR tiling code
  (`src/fastapi/app/services/ingest/image_tiling.py`, landed round 1,
  hardened round 2) narrowly overrides Pillow's decompression-bomb guard to
  handle real oversized scans — round 2 explicitly asked Codex to confirm
  that override was scoped narrowly rather than global. **Multiple of the
  open Pillow CVEs are bypasses of that exact guard through code paths
  outside `Image.open()`** (font loaders, `PdfStream.decode()`, the mmap
  path). Bumping Pillow to the patched version is the single highest-value
  fix here because it closes a gap in a control this codebase is already
  relying on. Check current pin in `src/fastapi/pyproject.toml`, bump to the
  first version with all of these CVEs fixed, run the full suite (tiling
  tests especially — `test_image_tiling.py`), and re-verify the narrow-scope
  override still behaves as intended against the real oversized-TIFF fixture
  used in round 2's end-to-end check.
- **starlette / python-multipart / urllib3 / pyjwt** sit directly in the
  FastAPI request path (auth, uploads, outbound HTTP) — bump these next.
  Check `pyjwt` usage specifically: if this codebase ever accepts a JWT with
  an algorithm chosen from the token header without pinning `algorithms=`
  server-side, the key-confusion CVE is exploitable; grep call sites and fix
  the vulnerable pattern in code even if bumping the library alone doesn't
  fully close it.
- **mcp** — check whether any of georag's own code runs an MCP server/client
  in a network-reachable configuration (vs. only used by Claude Code /
  Codex tooling locally, which wouldn't ship to Azure). If it's dev-tooling
  only, note that explicitly rather than treating it as a production-path fix.
- **transformers RCE** — this is a real concern given the embedding/reranker
  sidecars load HF models; confirm model loading only ever points at
  known/pinned model IDs (not user-controlled paths) and bump the pin
  regardless.
- **axios / form-data / shell-quote / ws / vite** — frontend-only; bump via
  `npm audit fix` / manual `package.json` bumps, confirm `npm run build` and
  Vitest still pass.
- **lxml, Mako, pyasn1, soupsieve, cryptography** — bump-and-verify; lower
  urgency but still real advisories on a branch headed to internet-facing
  Azure hosting.

For each bump: update the pin, regenerate the relevant lockfile
(`uv.lock` / `composer.lock` / `package-lock.json`), run the full suite, and
report before/after Dependabot alert counts (re-query
`gh api repos/kjmaguire/GeoRag-Intelligent-V2.1/dependabot/alerts` after
pushing — GitHub takes a few minutes to re-scan). If a bump requires a major
version jump with breaking API changes, don't force it silently — flag it
as a separate decision rather than papering over a broken test.

### 2. Merge or finish `feature/storage-abstraction`

This branch (`georag_object_storage` package + Laravel `StorageService`
facade, replacing direct MinIO/S3 SDK calls scattered across the codebase)
has 9 commits and was previously reported complete, but **is not merged
into `trim/phase-a-deletions`** — verified via
`git merge-base --is-ancestor origin/feature/storage-abstraction
origin/trim/phase-a-deletions` returning false. A prior note flagged one
piece ("PR6 docker-compose.yml env-var cleanup") as unsafe/deferred pending
a running-stack test. Determine current status:
- If storage-abstraction is safe and just never got merged, merge it into
  this branch, resolve any conflicts (this branch has moved a lot since
  storage-abstraction was cut — expect conflicts in `docker-compose.yml`,
  `pyproject.toml`, and anywhere ingest code touches S3/bronze storage
  directly), and run the full suite.
- If it's genuinely blocked on something (the deferred PR6 issue, or a real
  incompatibility with work landed since), say so explicitly rather than
  force-merging past a real conflict.

### 3. Laravel/frontend major dependency bumps (round 2's item 5, still open)

Round 2's diff touched `composer.json`/`package.json` by only ~4 lines
total — the major-version audit from round 2's §5 didn't happen. Same ask:
check for safe patch/minor bumps within the existing major-version
constraint (`laravel/framework ^13`, `inertiajs/inertia-laravel ^3`,
`react ^19`, `@inertiajs/react ^3.5`, `typescript ^6.0.2`, `vite ^8`,
`tailwindcss ^4.3`), apply those, and separately **flag** (don't apply)
any available major bump for Kyle to decide on.

### 4. Kestra retirement leftovers

A7 (Kestra retirement) is marked done, but these files still reference it
and weren't swept:
- `database/raw/phase3/10-kestra-role-and-db.sql` and
  `database/raw/phase3/95-kestra-sunset.sql` — check whether the sunset
  migration already ran against the live DB; if so these are historical
  migration records and should stay (don't delete applied migrations), but
  confirm neither is still being applied to fresh DB bootstraps in a way
  that recreates Kestra's role/db for no consumer.
- `src/fastapi/app/services/dispatchers/kestra.py` (plus its stale
  `__pycache__/*.pyc` siblings — delete those regardless, they're orphaned
  bytecode) — confirm zero callers (grep for `dispatchers.kestra` and
  `KestraDispatcher` or equivalent import name) before deleting the module
  itself.
- `docs/phase_g_followup_kestra_pagerduty_wired.md` — if Kestra is fully
  gone, this doc describes dead integration; either delete it or add a
  header noting it's historical/superseded so nobody re-implements it by
  mistake.

### 5. PHPStan baseline — further real reduction

Currently 162 findings in `phpstan-baseline.neon` (round 2 already did a
legitimate 216-line/real-fix reduction; this is what's left). Same
instruction as round 2: fix what's genuinely fixable, don't pad the count
down by converting real findings into broader suppressions. If a chunk of
the remainder is one repeated pattern (e.g. a specific generic-type gap),
say so and fix the pattern once rather than 40 individual baseline entries.

### 6. TODO/FIXME/XXX triage

Only 8 markers remain in `src/fastapi/app` (down from round 2's "55 at last
count" — most were already swept as part of the OCR/docling/paddleocr
deletions). Do a final pass: for each of the 8, either it's a real tracked
gap (leave it, and if it references a since-closed task number, update the
reference) or stale noise from finished work (remove the comment). List
each of the 8 by file:line in the PR description so Kyle can spot-check.

---

## What NOT to touch

Same as always: the live stack at `C:\Users\GeoRAG\Herd\georag`, and
anything already load-bearing per the original trim plan (laravel-reverb,
laravel-horizon, hatchet-worker-ai, the relocated `pdf_report.py` parser).
Multi-clone Docker safety: `-p georagintelligencev10 --env-file <abspath>
--no-deps`, never `--remove-orphans`.

Don't bump a dependency major version silently to close a Dependabot
alert if the fix is actually available within the current major — check
changelogs, not just "latest tag."

## Reporting back

Same as rounds 1-2: PR against `trim/phase-a-deletions` (or against PR #97's
branch if that's still open — Kyle will decide which to merge first), full
test evidence pasted in, before/after Dependabot alert counts for item 1,
and any genuine ambiguity (especially on item 2's merge conflicts, and
item 4's "is this migration still applied on fresh bootstrap") surfaced
explicitly rather than silently resolved.
