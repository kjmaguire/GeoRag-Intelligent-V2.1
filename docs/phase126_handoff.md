## Doc-phase 126 handoff — Import-gate extended to Dagster

**Status:** Live. **Both fastapi + dagster pass the gate, 0 uncovered each.**

## What landed

### Extended `scripts/check_pyproject_covers_imports.py`

The doc-phase 125 script already accepted positional CLI args
`[pyproject_path] [app_dir]`. Reused unchanged — the same script
now serves both targets. Only the `MODULE_TO_DISTRIBUTION` and
`ALLOWED_NON_PYPROJECT` lookup tables grew:

**New module → distribution mappings:**
- `psycopg2 → psycopg2-binary` (Dagster's sync Postgres driver;
  the `-binary` variant is the only one declared)
- `opentelemetry → opentelemetry-api` (umbrella name covering
  `-api`, `-sdk`, `-exporter-otlp-proto-http` — all declared)

**New allow-list additions:**

| Type | Members |
|---|---|
| Transitives | `shapely`, `pyogrio` (via geopandas), `charset_normalizer` (via requests), `pydantic` (universal) |
| Soft-deps with try/except | `langdetect`, `pdf2image`, `pytesseract` — pdf_report.py:920 even logs `"install with: pip install pdf2image pytesseract"` when absent |

### Caught one real undeclared dep — `rapidfuzz`

`georag_dagster/parsers/_hole_id.py` hard-imports `from rapidfuzz
import fuzz` for fuzzy collar-ID matching (no try/except). Was
NOT declared in dagster's pyproject. Gate failed at first run with
this finding.

Added to `src/dagster/pyproject.toml`:

```toml
# Doc-phase 126 — used hard by parsers/_hole_id.py for fuzzy-match
# collar-ID resolution. Was not previously declared; the
# check_pyproject_covers_imports gate caught the omission.
"rapidfuzz>=3.0",
```

This is exactly the class of bug the gate is designed to catch.
At the next dagster image rebuild, `rapidfuzz` would have been
absent and `_hole_id.py` would crash on import. Now declared.

### Wired into pre-commit + CI

**`.pre-commit-config.yaml`** — new `pyproject-covers-imports-dagster`
hook triggering on `src/dagster/pyproject.toml` or
`src/dagster/georag_dagster/**.py` changes.

**`.github/workflows/ci.yml`** — split the existing single step into
two named steps, one per target:
- `Pyproject covers app imports (FastAPI)`
- `Pyproject covers app imports (Dagster)`

### Final gate state

```
=== FastAPI ===
App imports scanned:    68
Pyproject declared:     37
Allow-listed:           22
Uncovered:              0
OK

=== Dagster ===
App imports scanned:    64
Pyproject declared:     36
Allow-listed:           22
Uncovered:              0
OK
```

## Why one script + shared allow-list (not per-target configs)

Initial instinct was per-target config files. Decided against:
- Same Python project conventions across both → same allow-list
  rules apply (transitives, soft-deps)
- Single script + lookup tables = single source of truth for the
  conventions; easier to keep consistent
- If we ever need divergent rules, adding `--config` parameter is
  cheap

Two targets share one script + 22 allow-listed names + 26
module→dist mappings. The few additions specific to dagster
(psycopg2-binary mapping, langdetect/pdf2image/pytesseract
soft-deps) are universally true — would never be incorrect for
fastapi to also see them.

## Cumulative incident-prevention surface

Pre-commit + CI gates triggered by source changes now include:

| Gate | What it catches |
|---|---|
| `system-prompt-version-bump` | Silent prompt edits without bumping `_SYSTEM_PROMPT_VERSION` |
| `fastapi-pydantic-freshness` | Staged fastapi files newer than the running container (stale Pydantic models) |
| `pyproject-covers-imports` | FastAPI imports that aren't in pyproject (doc-phase 122 incident class) |
| `pyproject-covers-imports-dagster` | Same drift class on the Dagster side |

## Carry-overs

Same as 125. Eventually:
- Composer.json side for Laravel — same incident pattern could hit there
- A v2 of the script could verify the module → dist mapping against
  the actually-installed environment via
  `importlib.metadata.packages_distributions()` and eliminate the
  hand-maintained `MODULE_TO_DISTRIBUTION` dict.
