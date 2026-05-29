## Doc-phase 125 handoff — Incident-driven CI gate for pyproject-vs-imports drift

**Status:** Live + wired into pre-commit + CI. Direct response to the
doc-phase 122 rebuild incident.

## What landed

### `scripts/check_pyproject_covers_imports.py`

Pure-stdlib Python script that:

1. AST-scans every `*.py` under `src/fastapi/app/` for top-level imports
2. Maps each imported module name → expected PyPI distribution name
   via the `MODULE_TO_DISTRIBUTION` dict (handles fitz↔pymupdf,
   docx↔python-docx, jwt↔pyjwt, etc.)
3. Reads `dependencies = [...]` from `src/fastapi/pyproject.toml`
4. Normalizes both sides to PyPI canonical form (lowercase + hyphens)
5. Fails if any imported module is uncovered by either pyproject
   declaration OR the `ALLOWED_NON_PYPROJECT` allow-list

Exit code 0 = covered. Non-zero = uncovered findings with actionable
"Fix options" output.

Today's run against the current state:

```
App imports scanned:    68
Pyproject declared:     37
Allow-listed:           15
Uncovered:              0
OK  every app/ import is covered by pyproject (or allow-listed).
```

### Allow-list — `ALLOWED_NON_PYPROJECT`

15 entries covering 4 categories:

| Category | Members |
|---|---|
| Installed via explicit Dockerfile `RUN pip install` | `slowapi`, `pytest`, `pytest_asyncio` |
| langgraph extra (explicit install in Dockerfile) | `langgraph`, `langgraph_checkpoint_postgres`, `langchain_mcp_adapters`, `langfuse` |
| Common transitives (guaranteed by parents) | `PIL` (pillow), `boto3` (via aioboto3), `numpy` (via torch+pandas+...), `pandas` (via geopandas), `starlette` (via fastapi), `logfire` (via pydantic-ai), `prometheus_client` (via prometheus-fastapi-instrumentator) |
| Cross-package soft-dep | `georag_dagster` — fastapi optionally imports from the Dagster sibling package (worker.py wraps in try/except; ingest_pdf does runtime import). ADR-0002 intentional pattern. |

### Module-name → distribution overrides — `MODULE_TO_DISTRIBUTION`

24 entries handling the names that diverge between `import xxx` and
`pip install xxx`. Examples:

- `fitz` → `pymupdf`
- `cv2` → `opencv-python`
- `docx` → `python-docx`
- `sklearn` → `scikit-learn`
- `pdfminer` → `pdfminer.six`
- `jwt` → `pyjwt`
- `sentry_sdk` → `sentry-sdk`
- `qdrant_client` → `qdrant-client`
- `hatchet_sdk` → `hatchet-sdk`

### Pre-commit integration

Appended to `.pre-commit-config.yaml`:

```yaml
- id: pyproject-covers-imports
  name: Verify every app/ import is declared in pyproject (or allow-listed)
  entry: python3 scripts/check_pyproject_covers_imports.py
  language: system
  pass_filenames: false
  files: ^(src/fastapi/pyproject\.toml|src/fastapi/app/.*\.py)$
  stages: [pre-commit]
```

Triggers on any `pyproject.toml` change OR any `app/*.py` change.
Pre-commit will block the commit if a new import isn't covered.

### CI integration

Added to `.github/workflows/ci.yml` Ruff job:

```yaml
- name: Pyproject covers app imports
  run: python3 scripts/check_pyproject_covers_imports.py
```

Runs on every PR. Pure-stdlib, <1 second execution. Zero new
dependencies needed in CI.

## Incident reference

This gate directly addresses the doc-phase 122 rebuild incident
(`docs/phase122_rebuild_incident.md`):

> The doc-phase 122 image rebuild succeeded (`exit 0`), but the new
> container failed health checks on startup:
>
>     ModuleNotFoundError: No module named 'sentry_sdk'
>
> Investigation revealed the working-tree pyproject.toml had drifted
> from HEAD: sentry-sdk[fastapi] removed, but main.py:41 still
> imports it. Two further missing deps (hatchet-sdk, aioboto3) +
> pymupdf surfaced only after additional rebuilds.

With this gate live, any future PR that:
1. Adds a new import to app/ without declaring its dist in pyproject, OR
2. Removes a dist from pyproject without removing the corresponding
   import from app/,
will fail at PR-time, not at runtime after a 15-minute rebuild.

## How to maintain the allow-list

When a legitimate uncovered import appears, choose the right home:

| Situation | Fix |
|---|---|
| Module imports under a different name than its dist (e.g. `from fitz` for pymupdf) | Add to `MODULE_TO_DISTRIBUTION` |
| Dist is installed via explicit `RUN pip install` in the Dockerfile | Add to `ALLOWED_NON_PYPROJECT` |
| Module is transitive via a parent we DO declare (guaranteed-co-installed) | Add to `ALLOWED_NON_PYPROJECT` with a comment |
| New direct dep | **Add to pyproject `dependencies = [...]`** — this is what the gate exists to enforce |

## Carry-overs

- Eventually extend the same gate to `src/dagster/` and Laravel
  composer.json side. Same drift pattern is possible there.
- A v2 enhancement could verify the import-name → distribution-name
  mapping against the actually-installed environment (using
  `importlib.metadata.packages_distributions()`), removing the need
  for the hand-maintained MODULE_TO_DISTRIBUTION dict.
