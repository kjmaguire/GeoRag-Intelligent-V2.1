#!/usr/bin/env python3
"""Verify every top-level import in app/ is declared in pyproject.toml.

Doc-phase 125 — incident-driven CI gate.

The doc-phase 122 rebuild crashed at runtime because main.py imports
sentry_sdk but `sentry-sdk[fastapi]` had been removed from
pyproject.toml. Two further missing deps (hatchet-sdk, aioboto3) +
pymupdf surfaced only after a second rebuild. Each round burned
~20 minutes of build time + diagnosis.

This script catches that drift at PR time:
- AST-scans every `*.py` under app/ for top-level imports
- Maps the imported names → known PyPI distribution names (some
  modules don't match their package name — fitz=pymupdf, etc.)
- Cross-references against the dependencies array in pyproject.toml
- Exits non-zero on any uncovered import

Usage:
    python scripts/check_pyproject_covers_imports.py [path/to/pyproject.toml] [path/to/app/]

Defaults to src/fastapi/pyproject.toml + src/fastapi/app/.

Run from repo root; designed to be hooked into CI + pre-commit.
"""
from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path


# ---------------------------------------------------------------------------
# Module-name → PyPI-distribution-name overrides
#
# Add to this dict when a package imports under a different name than its
# pip distribution. The script emits a clear "uncovered:" line on first
# encounter so you know whether to add to this dict or to pyproject.
# ---------------------------------------------------------------------------
MODULE_TO_DISTRIBUTION: dict[str, str] = {
    "fitz": "pymupdf",
    "cv2": "opencv-python",          # or opencv-python-headless
    "PIL": "pillow",
    "yaml": "pyyaml",
    "sklearn": "scikit-learn",
    "docx": "python-docx",
    "OpenSSL": "pyopenssl",
    "dateutil": "python-dateutil",
    "jose": "python-jose",
    "magic": "python-magic",
    "google": "google-cloud-storage",  # heuristic; rarely needed
    "pkg_resources": "setuptools",
    "_pytest": "pytest",
    "tomllib": "<stdlib>",
    "hatchet_sdk": "hatchet-sdk",
    "qdrant_client": "qdrant-client",
    "sentence_transformers": "sentence-transformers",
    "pydantic_ai": "pydantic-ai",
    "pydantic_settings": "pydantic-settings",
    "prometheus_client": "prometheus-client",
    "prometheus_fastapi_instrumentator": "prometheus-fastapi-instrumentator",
    "langchain_mcp_adapters": "langchain-mcp-adapters",
    "langgraph_checkpoint_postgres": "langgraph-checkpoint-postgres",
    "sentry_sdk": "sentry-sdk",
    "pdfminer": "pdfminer.six",
    "jwt": "pyjwt",
    # Doc-phase 126 — Dagster-side import-name → dist mappings
    "psycopg2": "psycopg2-binary",
    "opentelemetry": "opentelemetry-api",  # umbrella name; -api/-sdk both export `opentelemetry`
}


# ---------------------------------------------------------------------------
# Allow-list of modules satisfied by something other than pyproject `dependencies`
#
# Examples:
#   - Stdlib (handled by sys.stdlib_module_names check)
#   - Installed by an explicit `RUN pip install` in the Dockerfile, not
#     by pyproject (slowapi, pytest)
#   - Optional/extra-installed via the langgraph extra
# ---------------------------------------------------------------------------
ALLOWED_NON_PYPROJECT: set[str] = {
    # Installed via explicit Dockerfile RUN pip install lines
    "slowapi",
    "pytest",
    "pytest_asyncio",
    # langgraph chain — opt-in extra; explicitly installed in Dockerfile
    "langgraph",
    "langgraph_checkpoint_postgres",
    "langchain_mcp_adapters",
    "langfuse",
    # Common transitives that are effectively always present in any
    # ML/data stack — guaranteed to be installed alongside their parents
    "PIL",           # via pillow → matplotlib, weasyprint, opencv
    "boto3",         # via aioboto3
    "numpy",         # via torch, pandas, geopandas, xgboost, shap, ...
    "pandas",        # via geopandas, shap
    "starlette",     # via fastapi
    "logfire",       # via pydantic-ai 1.x (auto-instrumentation)
    "prometheus_client",  # via prometheus-fastapi-instrumentator
    "pydantic",      # universal; fastapi declares explicitly, dagster gets it via dagster
    # Cross-package soft-dep — fastapi optionally calls into the Dagster
    # sibling package via lazy/try-imports (see worker.py + ingest_pdf.py).
    # Pattern is intentional per ADR-0002.
    "georag_dagster",
    # Doc-phase 126 — Dagster-side additions:
    # Transitives guaranteed by parent deps:
    "shapely",           # via geopandas
    "pyogrio",           # via geopandas (replaces fiona)
    "charset_normalizer",  # via requests
    # Soft-deps with explicit try/except handling in the source:
    "langdetect",        # pdf_report.py wraps `from langdetect import DetectorFactory`
    "pdf2image",         # pdf_report.py:917 — gracefully degrades on absence
    "pytesseract",       # pdf_report.py:918 — same pattern as pdf2image
}


def _normalize(name: str) -> str:
    """Lowercase + replace underscores with hyphens (PyPI canonical form)."""
    return re.sub(r"[_.]+", "-", name).lower()


def collect_imports(app_dir: Path) -> set[str]:
    """Walk app_dir + return set of top-level imported module names."""
    imports: set[str] = set()
    for p in app_dir.rglob("*.py"):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        except (SyntaxError, UnicodeDecodeError) as e:
            print(f"WARN  could not parse {p}: {e}", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    imports.add(n.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    imports.add(node.module.split(".")[0])
    return imports


def collect_declared_distributions(pyproject: Path) -> set[str]:
    """Return the set of normalized PyPI distribution names declared in
    pyproject's main `dependencies` array.
    """
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    deps = data.get("project", {}).get("dependencies", [])
    distributions: set[str] = set()
    for spec in deps:
        # Strip [extras], version pins, and whitespace
        name = re.split(r"[\[<>=!~;\s]", spec, maxsplit=1)[0].strip()
        if name:
            distributions.add(_normalize(name))
    return distributions


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    pyproject_path = Path(
        sys.argv[1] if len(sys.argv) > 1 else repo_root / "src/fastapi/pyproject.toml"
    )
    app_dir = Path(
        sys.argv[2] if len(sys.argv) > 2 else repo_root / "src/fastapi/app"
    )

    if not pyproject_path.exists():
        print(f"FAIL  pyproject.toml not found: {pyproject_path}", file=sys.stderr)
        return 2
    if not app_dir.exists():
        print(f"FAIL  app/ directory not found: {app_dir}", file=sys.stderr)
        return 2

    imports = collect_imports(app_dir)
    declared = collect_declared_distributions(pyproject_path)
    stdlib = set(sys.stdlib_module_names)

    uncovered: list[tuple[str, str]] = []   # (import_name, expected_distribution_name)
    for mod in sorted(imports):
        if mod in stdlib:
            continue
        if mod == "app":
            continue
        if mod in ALLOWED_NON_PYPROJECT:
            continue
        # Map to expected distribution name
        expected = MODULE_TO_DISTRIBUTION.get(mod, mod)
        if expected == "<stdlib>":
            continue
        expected_norm = _normalize(expected)
        # The import is covered if either:
        # - The expected distribution name is declared in pyproject
        # - The import's own normalized form matches a declared distribution
        candidates = {expected_norm, _normalize(mod)}
        if candidates & declared:
            continue
        uncovered.append((mod, expected))

    print(f"App imports scanned:    {len(imports)}")
    print(f"Pyproject declared:     {len(declared)}")
    print(f"Allow-listed:           {len(ALLOWED_NON_PYPROJECT)}")
    print(f"Uncovered:              {len(uncovered)}")
    print()

    if uncovered:
        print("FAIL  the following app/ imports are NOT covered by pyproject:")
        for mod, expected in uncovered:
            if mod == expected:
                print(f"  - import '{mod}' → no matching dist in pyproject")
            else:
                print(f"  - import '{mod}' → expected dist '{expected}', "
                      "not in pyproject")
        print()
        print("Fix options:")
        print("  1. Add the dist name to `dependencies = [...]` in pyproject.toml")
        print("  2. If installed via Dockerfile RUN pip install, add to")
        print(f"     ALLOWED_NON_PYPROJECT in {Path(__file__).name}")
        print("  3. If module name differs from PyPI name, add to")
        print(f"     MODULE_TO_DISTRIBUTION in {Path(__file__).name}")
        return 1

    print("OK  every app/ import is covered by pyproject (or allow-listed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
