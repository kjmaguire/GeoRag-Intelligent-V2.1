#!/usr/bin/env bash
# scripts/check_audit_invariants_python.sh — REC#3 (2026-06-03)
#
# Runs the Python file-content audit invariants locally before commit.
# Same set runs in .github/workflows/audit-invariants.yml on every PR.
#
# Execution model
# ---------------
# Two paths:
#   1. docker exec georag-fastapi (preferred when the dev container
#      is up — matches CI environment exactly)
#   2. host pytest (fallback for environments without docker)
#
# Speed budget: under 30s. File-content checks only — no live DB / LLM.
#
# Adding a new invariant
# ----------------------
# Mirror the PHP wrapper: add the test path to PYTHON_INVARIANTS below,
# register in docs/AUDIT_INVARIANTS.md, add to the CI workflow's
# python-invariants job test list.

set -euo pipefail

PYTHON_INVARIANTS=(
    "tests/test_acquire_scoped.py"
    "tests/test_dead_settings_tagged.py"
    "tests/test_ingest_zip_archive_observability.py"
    "tests/test_shadow_trigger_observability.py"
    "tests/test_source_trust_boost_wiring.py"
    "tests/test_vllm_payload_shape.py"
    "tests/test_workspace_context.py"
    "tests/test_workspace_context_b4_centralisation.py"
    "tests/test_workspace_dependency.py"
    "tests/test_scoped_connection.py"
    "tests/test_lookup_and_rescope.py"
)

# Verify every entry exists. Drift = a test file deleted but the
# wrapper still references it; "no tests ran" would be a silent pass.
for f in "${PYTHON_INVARIANTS[@]}"; do
    if [[ ! -f "src/fastapi/$f" ]]; then
        echo "audit-invariants: missing test file src/fastapi/$f" >&2
        echo "  Either restore it (the bug class it pinned shipped before)" >&2
        echo "  or remove the path from $0 AND docs/AUDIT_INVARIANTS.md." >&2
        exit 1
    fi
done

# Prefer the running dev container — its dep tree is the live tree,
# its imports match production. Falls back to host pytest if the
# container isn't up (eg. a contributor doing pre-commit on the host
# without docker running).
if docker exec georag-fastapi true 2>/dev/null; then
    docker exec georag-fastapi sh -c "cd /app && python -m pytest -q ${PYTHON_INVARIANTS[*]}"
else
    echo "audit-invariants: georag-fastapi container not up — falling back to host pytest"
    cd src/fastapi
    python -m pytest -q "${PYTHON_INVARIANTS[@]}"
fi
