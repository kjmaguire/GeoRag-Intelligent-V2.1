#!/usr/bin/env bash
# =============================================================================
# fe_container_validate.sh — validate the Inertia/Vite frontend in a container
# =============================================================================
# This box has no local Node (Docker-only app), so frontend changes are
# validated inside a node:NN container that mirrors what CI + the Laravel image
# run: `npm ci` -> `tsc --noEmit` -> `vite build` -> `vitest`.
#
# It copies ONLY the frontend build inputs (configs + resources/, a few MB) into
# a container-local dir — NOT the whole repo (src/ alone is ~1.8 GB of Python
# and tar-copying it through the Docker Desktop FS bridge is unusably slow).
# A persistent npm-cache volume keeps repeat runs fast.
#
# Usage (from repo root, Git Bash on Windows):
#   MSYS_NO_PATHCONV=1 docker run --rm \
#     -v "$(pwd -W):/src:ro" -v georag_npm_cache:/root/.npm \
#     node:24 bash //src/scripts/ops/fe_container_validate.sh
#
# On Linux/macOS drop MSYS_NO_PATHCONV and the //src double-slash:
#   docker run --rm -v "$PWD:/src:ro" -v georag_npm_cache:/root/.npm \
#     node:24 bash /src/scripts/ops/fe_container_validate.sh
#
# Prints the tsc error count, build result, and vitest tally. NOTE: the repo
# currently carries pre-existing tsc errors that `vite build` tolerates (Vite
# strips types via esbuild, so build can be green while tsc is not). When
# checking whether YOUR change is clean, compare the tsc count before/after —
# a net-zero delta means your change is tsc-neutral. CI's frontend job gates on
# `tsc --noEmit`, so a non-zero count is a real (pre-existing) CI failure.
# =============================================================================
set +e
mkdir -p /build && cd /src
echo "### copying frontend inputs ###"
cp package.json package-lock.json tsconfig.json vite.config.ts components.json /build/ 2>/dev/null
for f in tailwind.config.ts tailwind.config.js postcss.config.js eslint.config.js vitest.config.ts; do
  [ -f "$f" ] && cp "$f" /build/
done
cp -r resources /build/resources

cd /build
echo "### npm ci on $(node --version) ###"
npm ci --no-audit --no-fund --prefer-offline > /tmp/ci.log 2>&1 \
  && echo "CI_OK" || { echo "CI_FAIL"; tail -25 /tmp/ci.log; exit 1; }

echo "### tsc --noEmit ###"
npx tsc --noEmit > /tmp/tsc.txt 2>&1
echo "TSC_ERRORS=$(grep -c 'error TS' /tmp/tsc.txt)"
grep 'error TS' /tmp/tsc.txt | head -40

echo "### vite build ###"
npm run build > /tmp/build.log 2>&1 \
  && echo "BUILD_OK" || { echo "BUILD_FAIL"; tail -15 /tmp/build.log; }

echo "### vitest ###"
npm run test > /tmp/test.txt 2>&1
grep -E 'Tests ' /tmp/test.txt | tail -1
