#!/usr/bin/env bash
# Discrimination tests for scripts/check_silent_exception_handlers.py.
#
# A ratchet that only ever prints OK against a tree someone has already
# baselined has demonstrated nothing -- which is the exact failure this
# repository keeps finding in its own gates. Each case below asserts the
# checker's VERDICT on a handler shape, so a definition that quietly stops
# recognising `logger.warning(...)` as speaking, or starts treating
# `except: pass` as fine, fails here rather than silently letting the
# baseline drift up.
#
# Run: bash scripts/tests/silent_exception_handlers_test.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHECKER="${REPO_ROOT}/scripts/check_silent_exception_handlers.py"
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python

PASS=0
FAIL=0

# The checker walks fixed roots under the repo, so verdicts are tested
# against its _speaks() predicate directly rather than by planting files
# in src/ -- planting them would pollute the very baseline under test.
verdict() {
    # verdict <label> <expect: silent|speaks> <handler source>
    local label="$1" want="$2" src="$3" got
    got="$("$PYTHON" - "$CHECKER" "$src" <<'PY'
import ast, importlib.util, sys

spec = importlib.util.spec_from_file_location("checker", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

tree = ast.parse(sys.argv[2])
handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
if len(handlers) != 1:
    print(f"expected one handler, parsed {len(handlers)}")
    raise SystemExit(1)
print("speaks" if mod._speaks(handlers[0]) else "silent")
PY
)"
    if [ "$got" = "$want" ]; then
        printf 'ok   %s -> %s\n' "$label" "$got"; PASS=$((PASS + 1))
    else
        printf 'FAIL %s: expected %s, got %s\n' "$label" "$want" "$got"; FAIL=$((FAIL + 1))
    fi
}

verdict "bare pass"              silent 'try:
    x()
except Exception:
    pass'

verdict "swallow into a default" silent 'try:
    x()
except Exception:
    result = None'

verdict "comment only"           silent 'try:
    x()
except Exception:
    # best effort
    result = 0'

verdict "logger.warning"         speaks 'try:
    x()
except Exception:
    logger.warning("nope")'

verdict "log.debug via alias"    speaks 'try:
    x()
except Exception:
    log.debug("nope", exc_info=True)'

verdict "re-raise"               speaks 'try:
    x()
except Exception:
    raise'

verdict "re-raise as another"    speaks 'try:
    x()
except ValueError as exc:
    raise RuntimeError("bad") from exc'

verdict "self._log_failure"      speaks 'try:
    x()
except Exception:
    self._log_failure(exc_info=True)'

verdict "sentry capture"         speaks 'try:
    x()
except Exception:
    sentry_sdk.capture_exception()'

verdict "raise nested in an if"  speaks 'try:
    x()
except Exception:
    if strict:
        raise'

# --- the ratchet itself ----------------------------------------------
# A missing baseline must be an error, not an implicit pass: a checker
# that reports success because it could not find its own reference file
# is the shape this whole script exists to prevent.
TMP_HOME="$(mktemp -d)"
trap 'rm -rf "$TMP_HOME"' EXIT
if "$PYTHON" - <<PY >/dev/null 2>&1
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("checker", r"${CHECKER}")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.BASELINE_PATH = pathlib.Path(r"${TMP_HOME}") / "nope.json"
sys.exit(mod.main([]))
PY
then
    printf 'FAIL a missing baseline exited 0\n'; FAIL=$((FAIL + 1))
else
    printf 'ok   a missing baseline is a non-zero exit\n'; PASS=$((PASS + 1))
fi

# And the real tree must currently be at or under its baseline.
if "$PYTHON" "$CHECKER" >/dev/null; then
    printf 'ok   the committed tree is within its baseline\n'; PASS=$((PASS + 1))
else
    printf 'FAIL the committed tree exceeds its baseline\n'; FAIL=$((FAIL + 1))
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
