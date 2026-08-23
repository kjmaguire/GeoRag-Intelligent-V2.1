#!/usr/bin/env bash
# Give the five probe-less Container Apps a liveness and readiness probe.
#
#   bash deploy/azure/containerapps/apply-probes.sh             # show the diff
#   bash deploy/azure/containerapps/apply-probes.sh --apply     # send it
#   bash deploy/azure/containerapps/apply-probes.sh --apply --horizon
#
# Runs from Git Bash, WSL or native bash. See ../_host_compat.sh for the
# path and line-ending handling a Windows `az` needs.
#
# ---------------------------------------------------------------------
# WHAT IT DOES, AND WHY NOT `az containerapp update --yaml`
# ---------------------------------------------------------------------
# There is no `--probes` flag on `az containerapp update`, so the probes
# have to go through the template. The documented way to edit a template
# is to round-trip it:
#
#     az containerapp show -o yaml > app.yaml   # edit   # update --yaml
#
# Do not do that here. `show` emits a `configuration.secrets` block with
# the NAMES present and the values absent, and sending it back sets every
# one of those secrets to empty. That is the same trap apply-redis.sh
# exists to avoid, one layer up: there it was a placeholder string, here
# it would be nothing at all, and the blast radius is Redis auth, the
# service key and the ACR pull credential on one app.
#
# So this script PATCHes `properties.template` and nothing else. The
# template it sends is the app's CURRENT template with the probes (and,
# for the worker, three env vars) merged in, so the image, the resource
# sizes, the scale rules and every other env var travel through
# unchanged. `configuration` is never named in the body, so secrets,
# ingress and registries are not addressable by this script at all.
#
# ---------------------------------------------------------------------
# laravel-horizon-cc IS OPT-IN
# ---------------------------------------------------------------------
# The other four apps already listen on something a probe can reach.
# Horizon does not -- `php artisan horizon` serves no HTTP -- so its
# probe depends on the image carrying docker/horizon-entrypoint.sh AND
# the app's command pointing at it. Applying the probe before the image
# ships would configure a check that can only fail, and a failing
# liveness probe is a restart loop.
#
# The order is: merge the image change, let CD deploy it, then run this
# with --horizon. The script refuses to touch that app without the flag,
# and warns if the running image predates the entrypoint.
set -uo pipefail

RG=georag
SUBSCRIPTION=d314ab40-b5b7-4e3e-8308-86023fb7638a
API_VERSION=2026-01-01

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPEC="${HERE}/probes.json"

. "${HERE}/../_host_compat.sh"

APPLY=0
HORIZON=0
for arg in "$@"; do
    case "$arg" in
        --apply)   APPLY=1 ;;
        --horizon) HORIZON=1 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
    echo "python3 is required (JSON merge)" >&2
    exit 2
fi

if [ ! -f "$SPEC" ]; then
    echo "missing $SPEC" >&2
    exit 2
fi

APPS="$("$PY" -c "
import json, sys
spec = json.load(open(sys.argv[1], encoding='utf-8'))
print(' '.join(k for k in spec if not k.startswith('_')))
" "$SPEC")"

rc=0

for APP in $APPS; do
    if [ "$APP" = "laravel-horizon-cc" ] && [ "$HORIZON" -eq 0 ]; then
        echo "== ${APP}: skipped (needs --horizon, and the image must carry horizon-entrypoint.sh first)"
        continue
    fi

    CURRENT="$(mktemp -t probes-cur-XXXXXX.json)"
    MERGED="$(mktemp -t probes-new-XXXXXX.json)"
    # shellcheck disable=SC2064
    trap "rm -f '$CURRENT' '$MERGED'" EXIT

    if ! az containerapp show -g "$RG" -n "$APP" -o json > "$CURRENT" 2>/dev/null; then
        echo "== ${APP}: not found in resource group ${RG} -- skipped"
        rc=1
        continue
    fi

    # Merge, and report what changed. Exits 3 when the app is already in
    # the target state so the caller can skip the PATCH entirely.
    "$PY" - "$CURRENT" "$SPEC" "$APP" "$MERGED" <<'PYEOF'
import json
import sys

cur_path, spec_path, app, out_path = sys.argv[1:5]

with open(cur_path, encoding="utf-8") as fh:
    current = json.load(fh)
with open(spec_path, encoding="utf-8") as fh:
    spec = json.load(fh)[app]

template = current["properties"]["template"]
containers = template.get("containers") or []
if len(containers) != 1:
    print(f"   expected exactly one container, found {len(containers)}", file=sys.stderr)
    raise SystemExit(4)

container = containers[0]
changes = []

wanted_probes = spec["probes"]
if (container.get("probes") or []) != wanted_probes:
    kinds = ", ".join(p["type"].lower() for p in wanted_probes)
    have = len(container.get("probes") or [])
    changes.append(f"probes: {have} -> {len(wanted_probes)} ({kinds})")
    container["probes"] = wanted_probes

# Env vars are additive and keyed by name: anything already set on the app
# and not named in the spec is left exactly as it is.
env = container.get("env") or []
by_name = {e["name"]: e for e in env}
for wanted in spec.get("env", []):
    name, value = wanted["name"], wanted["value"]
    if by_name.get(name, {}).get("value") == value:
        continue
    changes.append(f"env {name}={value}" + (" (was unset)" if name not in by_name else ""))
    if name in by_name:
        by_name[name]["value"] = value
        by_name[name].pop("secretRef", None)
    else:
        env.append({"name": name, "value": value})
container["env"] = env

wanted_command = spec.get("command")
if wanted_command and container.get("command") != wanted_command:
    changes.append(f"command: {container.get('command')} -> {wanted_command}")
    container["command"] = wanted_command

if not changes:
    raise SystemExit(3)

for line in changes:
    print(f"   {line}")

# `az containerapp show` returns fields the PATCH API refuses to take
# back. `imageType` is one ("Unknown properties imageType in
# ContainerAppContainer are not supported") and it is emitted on every
# container. Strip rather than whitelist: a whitelist silently drops any
# field this script has not heard of, which on a template round-trip
# means losing live configuration.
for group in ("containers", "initContainers"):
    for entry in template.get(group) or []:
        entry.pop("imageType", None)

# Same story one level up: `customMetricsSettings` is returned by `show`
# and rejected by PATCH ("Unknown properties customMetricsSettings in
# ContainerAppTemplate are not supported"). revisionSuffix comes back as ""
# and would pin the new revision's name, so it goes too.
for read_only in ("customMetricsSettings", "revisionSuffix"):
    template.pop(read_only, None)

with open(out_path, "w", encoding="utf-8", newline="") as fh:
    json.dump({"properties": {"template": template}}, fh)
PYEOF
    merge_rc=$?

    if [ "$merge_rc" -eq 3 ]; then
        echo "== ${APP}: already correct"
        rm -f "$CURRENT" "$MERGED"
        continue
    fi
    if [ "$merge_rc" -ne 0 ]; then
        echo "== ${APP}: merge failed (rc=${merge_rc})"
        rc=1
        rm -f "$CURRENT" "$MERGED"
        continue
    fi

    echo "== ${APP}: changes above"

    if [ "$APPLY" -eq 0 ]; then
        rm -f "$CURRENT" "$MERGED"
        continue
    fi

    URL="https://management.azure.com/subscriptions/${SUBSCRIPTION}/resourceGroups/${RG}/providers/Microsoft.App/containerApps/${APP}?api-version=${API_VERSION}"
    # Inline, not --body @file. `az rest` does not expand a leading @
    # for this parameter -- it posts the literal string and ARM answers
    # "Unexpected character encountered while parsing value: @".
    PATCH_ERR="$(az rest --method PATCH --url "$URL" \
        --headers "Content-Type=application/json" \
        --body "$(cat "$MERGED")" 2>&1 >/dev/null)"
    if [ -z "$PATCH_ERR" ]; then
        echo "   applied"
    else
        echo "   FAILED: $(printf '%s' "$PATCH_ERR" | strip_cr | head -3)"
        rc=1
    fi

    rm -f "$CURRENT" "$MERGED"
done

if [ "$APPLY" -eq 0 ]; then
    echo
    echo "dry run -- nothing sent. Re-run with --apply."
    exit "$rc"
fi

# ---------------------------------------------------------------------
# Verify against the live resource rather than trusting the PATCH result.
# ---------------------------------------------------------------------
echo
echo "-- live probe count per app --"
az containerapp list -g "$RG" -o json 2>/dev/null | "$PY" -c "
import json, sys
for app in sorted(json.load(sys.stdin), key=lambda a: a['name']):
    probes = (app['properties']['template']['containers'][0].get('probes')) or []
    kinds = ','.join(sorted(p['type'] for p in probes)) or '-- none --'
    print(f\"{app['name']:24} {len(probes):2}  {kinds}\")
"

exit "$rc"
