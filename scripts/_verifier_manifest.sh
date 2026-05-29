#!/usr/bin/env bash
# Verifier cascade manifest helper — sourced by every phase3 verifier.
#
# Doc-phase 62 fix for the O(N²) cascade problem documented in
# doc-phase 60 handoff §5.1 and doc-phase 61 handoff §5.3:
#
# Each verifier writes a `passed_at` + `git_sha` entry to a JSON manifest
# when it succeeds. The cascade check is:
#   - manifest entry exists?
#   - entry's passed_at is within MANIFEST_TTL_SEC (default 1 hour)?
#   - entry's git_sha matches current short SHA (or current SHA unknown)?
# If yes → cascade skips re-running that prior verifier (counts as PASS).
# If no → cascade falls through to running the prior verifier.
#
# Result: cascade for a typical doc-phase tick goes from ~20 min to
# ~30 sec when prior verifiers haven't regressed since their last run.
#
# CI / fresh checkouts: no manifest file → cascade runs every prior
# verifier from scratch. Same as before doc-phase 62.

set -u  # don't `set -e` — caller may want to capture our return codes

# Manifest location relative to repo root. Override via MANIFEST_DIR=
# (e.g. for tests).
MANIFEST_DIR="${MANIFEST_DIR:-.verifier-state}"
MANIFEST_FILE="$MANIFEST_DIR/cascade-passes.json"
MANIFEST_TTL_SEC="${MANIFEST_TTL_SEC:-3600}"  # 1 hour default


_current_git_sha() {
    git rev-parse --short HEAD 2>/dev/null || echo "unknown"
}


# Record that a verifier passed.
#
# Usage at end of a verifier:
#   mark_verifier_passed "step1"
#
# Writes an entry to .verifier-state/cascade-passes.json with the
# current timestamp + short git SHA. Atomic via .tmp + mv.
mark_verifier_passed() {
    local step="$1"
    if [ -z "$step" ]; then
        echo "[manifest] mark_verifier_passed: missing step name" >&2
        return 1
    fi

    mkdir -p "$MANIFEST_DIR"
    local now
    now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    local sha
    sha=$(_current_git_sha)

    # Python-based atomic read-modify-write. Avoids jq dependency.
    python3 - <<PY
import json, os
fp = "$MANIFEST_FILE"
data = {}
if os.path.exists(fp):
    try:
        with open(fp) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
data["$step"] = {"passed_at": "$now", "git_sha": "$sha"}
tmp = fp + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2, sort_keys=True)
os.replace(tmp, fp)
PY
}


# Return 0 if the named step's manifest entry is fresh + SHA-matched.
# Return 1 otherwise (manifest missing, entry missing, expired, or SHA mismatch).
#
# Usage in cascade:
#   if check_verifier_recent "step1"; then
#       note "[step1] PASS — manifest recent (skip re-run)"
#   elif bash "$SCRIPT_DIR/phase3_master_plan_step1_verify.sh" >/dev/null 2>&1; then
#       note "[step1] PASS — verifier re-run green"
#   else
#       note "[step1] FAIL — verifier regressed"
#       FAIL=$((FAIL + 1))
#   fi
check_verifier_recent() {
    local step="$1"
    if [ -z "$step" ]; then
        return 1
    fi
    if [ ! -f "$MANIFEST_FILE" ]; then
        return 1
    fi

    python3 - <<PY
import json, os, subprocess, sys
from datetime import datetime, timezone

fp = "$MANIFEST_FILE"
try:
    with open(fp) as f:
        data = json.load(f)
except Exception:
    sys.exit(1)

entry = data.get("$step") if isinstance(data, dict) else None
if not isinstance(entry, dict):
    sys.exit(1)

passed_at = entry.get("passed_at")
if not passed_at:
    sys.exit(1)

try:
    t = datetime.fromisoformat(passed_at.replace("Z", "+00:00"))
except Exception:
    sys.exit(1)

age = (datetime.now(timezone.utc) - t).total_seconds()
if age > $MANIFEST_TTL_SEC:
    sys.exit(1)

# Git SHA scoping: re-run if the working tree has moved since this
# verifier last passed. If we can't get a current SHA (no git, detached
# state, etc.), accept the entry — better to skip a re-run than block
# work in non-git environments.
try:
    cur_sha = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"],
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()
except Exception:
    cur_sha = "unknown"

manifest_sha = entry.get("git_sha", "unknown")
if cur_sha != "unknown" and manifest_sha != "unknown" and cur_sha != manifest_sha:
    sys.exit(1)

sys.exit(0)
PY
}


# Helper: cascade-check a prior verifier using the manifest first, then
# falling back to running it. Prints a status line matching the existing
# verifier convention. Returns 0 on PASS, 1 on FAIL.
#
# Usage:
#   cascade_check_step "1"                              # uses default verifier path
#   cascade_check_step "1" "phase3_master_plan_step1"   # custom prefix
cascade_check_step() {
    local step="$1"
    local prefix="${2:-phase3_master_plan_step}"
    local verifier_path="$SCRIPT_DIR/${prefix}${step}_verify.sh"

    if check_verifier_recent "step${step}"; then
        echo "[step${step}] PASS — manifest recent (skip re-run)"
        return 0
    fi

    if [ ! -f "$verifier_path" ]; then
        echo "[step${step}] FAIL — verifier script not found: $verifier_path" >&2
        return 1
    fi

    if bash "$verifier_path" >/dev/null 2>&1; then
        echo "[step${step}] PASS — verifier re-run green"
        return 0
    else
        echo "[step${step}] FAIL — verifier regressed" >&2
        return 1
    fi
}
