# Shell compatibility for operator scripts that drive the Azure CLI.
#
# Sourced, not executed. Two problems, both caused by the same thing: on
# this project's Windows workstation `az` is a NATIVE WINDOWS binary, and
# it gets called from three different shells that each hand it arguments
# differently.
#
#   Git Bash   command -v az -> /c/Program Files/.../wbin/az
#   WSL        command -v az -> /mnt/c/Program Files/.../wbin/az   (interop)
#   Linux/CI   command -v az -> /usr/bin/az                        (native)
#
# 1. PATHS. A temp file at /tmp/x.yaml is a real path to the shell and
#    nothing at all to a Windows az.exe, which reports
#
#        ERROR: /tmp/redis-apply-ncKHLv.yaml does not exist
#
#    Git Bash usually hides this by rewriting the argument on the way
#    through -- but that rewrite is disabled by MSYS_NO_PATHCONV=1, which
#    create-alerts.sh requires for its /subscriptions/... resource IDs.
#    WSL never rewrites anything, so there it fails unconditionally.
#
# 2. LINE ENDINGS. A Windows az emits CRLF. Git Bash strips the CR;
#    WSL does not. So `az ... -o tsv` yields "redis-password\r", and every
#    downstream comparison silently fails:
#
#        grep -qx "redis-password"        <- no match, reports the secret gone
#        echo "# NOTE: '${name}' already" <- CR rewinds the cursor and the
#                                            start of the line is overwritten
#
# Both are invisible until they are not, and both produce failures that
# read as real problems with Azure rather than with the shell.

# The path form the local `az` can actually open.
#
# Converts only when az is a Windows binary. A native Linux az with a
# native path needs nothing, and converting for it would break it.
to_host_path() {
    _hc_path="$1"
    case "$(command -v az 2>/dev/null)" in
        /mnt/*)
            # WSL reaching the Windows CLI through interop.
            wslpath -w "$_hc_path" 2>/dev/null || printf '%s' "$_hc_path"
            ;;
        *)
            if command -v cygpath >/dev/null 2>&1; then
                # Git Bash / MSYS / Cygwin: az is always the Windows one.
                cygpath -w "$_hc_path"
            else
                printf '%s' "$_hc_path"
            fi
            ;;
    esac
}

# Filter for command substitution around a Windows az.
#
#   name="$(az ... -o tsv | strip_cr)"
strip_cr() {
    tr -d '\r'
}
