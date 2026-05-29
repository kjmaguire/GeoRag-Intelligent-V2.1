# Doc-phase 122 rebuild incident — uncommitted pyproject drift

**Status:** Caught + fixed mid-rebuild. Re-rebuild in progress with
corrected pyproject.

## What happened

The doc-phase 122 image rebuild succeeded (`exit 0`), but the new
`georag/fastapi:latest` container failed health checks on startup:

```
ModuleNotFoundError: No module named 'sentry_sdk'
  File "/app/app/main.py", line 41, in <module>
    import sentry_sdk
```

Investigation revealed the working-tree `src/fastapi/pyproject.toml`
had drifted from HEAD with **uncommitted wholesale changes**, not
just my §5/§7/§12 additions:

### Removed in working tree (vs HEAD)

- `sentry-sdk[fastapi]>=2.20` — but main.py:41 still calls
  `sentry_sdk.init(...)` at module load → fatal import error
- `pikepdf>=9.0`, `pypdfium2>=4.30`, `cachetools>=5.5` — §04p Phase 1.A
  PDF ingestion subsystem (every /pdf/* endpoint)
- `pdfminer.six>=20240706`, `pdfplumber>=0.11` — §04p Phase 1.B text +
  layout extraction (/pdf/extract_text, /pdf/find_tables)
- `docling>=2.13` — §04p Phase 1.C-i region detection (/pdf/find_legends)
- `paddlepaddle>=3.1`, `paddleocr>=2.10` — §04p Phase 1.C-ii OCR

### Downgraded in working tree (vs HEAD)

- `uvicorn[standard]`: `>=0.46.0` → `>=0.34.0`
- `pydantic-ai`: **`>=1.0` → `>=0.2,<0.3`** — **MAJOR version
  downgrade** with breaking API changes between 0.x and 1.x
- `redis`: `>=7.0` → `>=5.0`
- `qdrant-client`: `>=1.17` → `>=1.13`
- `neo4j`: `>=6.0` → `>=5.28`
- `sentence-transformers`: `[onnx]>=5.0` → `>=4.1` (lost the
  `[onnx]` extra → ONNX inference path would break)
- `anthropic`: `>=0.100` → `>=0.40`

### Cause

The downgrades + removals were uncommitted in the working tree
before this session began. The previous `georag/fastapi:latest`
image was built when these changes did NOT exist (HEAD versions
were in use), so the running container had all the right deps —
but the next rebuild would fail.

My §5/§7/§12 additions in doc-phase 122 didn't introduce the issue —
they just triggered the rebuild that surfaced it.

## Fix

```bash
# 1. Restore pyproject.toml to HEAD baseline
cd ~/projects/georag
git checkout HEAD -- src/fastapi/pyproject.toml

# 2. Re-apply ONLY the §5/§7/§12 additions on top
#    (programmatic injection — see scripts/inject_phase122_deps.py if
#    we land that helper later)

# 3. Verify diff is pure additions
git diff HEAD src/fastapi/pyproject.toml | grep -E '^[+-]' | grep -v '^---' | grep -v '^+++'
# Expect only + lines for the 9 new deps
```

After the fix, the pyproject diff vs HEAD is **+9 deps, 0 removals,
0 downgrades**. Diff length: 23 lines added (deps + comments).

## Carry-overs

1. **WHY were those downgrades in the working tree?** They predate
   this session. Investigate when the next session starts — could be:
   - An aborted "downgrade to LTS" refactor branch that was applied
     locally but not committed
   - Drift from a Dagster-side dep alignment that bled into the
     FastAPI pyproject
   - An accidental `uv sync --upgrade` that got reverted partially
2. The `uv.lock` file (if present) may explain what was actually
   installed in the old container.
3. **Build a CI gate** that fails any PR where main.py imports a
   module that's not in pyproject's dependencies. Would have caught
   the sentry-sdk removal at PR time.

## Doc-phase tracking

Counts as part of doc-phase 122 (the rebuild attempt). The corrected
rebuild is doc-phase 122-fix; smoke test runs after it lands.

---

## Update — second-round corrections (rebuild #3)

The first restore-from-HEAD overshot. Working-tree pyproject had
**both** harmful drift (downgrades + §04p removals) AND useful net-new
adds that `git checkout HEAD --` dropped.

AST scan of `app/**/*.py` imports + `importlib.import_module` against
the built image surfaced 3 missing pip deps:

| Module | Used by | Action |
|---|---|---|
| `hatchet_sdk` | `app.hatchet_workflows.*` | pip: `hatchet-sdk>=1.33` |
| `aioboto3` | `ingest_pdf` + `re_ocr_page` S3 helpers | pip: `aioboto3>=13.0` |
| `fitz` (= pymupdf) | §04p page-image rendering | pip: `pymupdf>=1.24` |

Plus one system-lib gap caught by direct `import paddleocr` test:

| System lib | Why | Action |
|---|---|---|
| `libGL.so.1` | OpenCV (paddleocr transitive) | apt: `libgl1` |
| `libglib2.0-0` | OpenCV / fontconfig | apt: `libglib2.0-0` |

Rebuild #3 incorporates all 5 fixes. CI gate (carry-over #3) would
prevent future occurrences.
