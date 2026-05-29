# =============================================================================
# scripts/fix-docker.ps1 — Docker Desktop recovery script for GeoRAG
# =============================================================================
#
# Recovers Docker Desktop from the recurring "engine won't start" failure
# mode we've hit ~3 times since 2026-05-18. Symptoms:
#
#   * `docker version`  → HTTP 500 from /v1.54/version
#   * Docker Desktop UI shows backend init looping with "still waiting for
#     init control API to respond after N minutes"
#   * `wsl -l -v` is missing the `docker-desktop` distribution
#   * `ls %LOCALAPPDATA%\Docker\run\` shows `-?????` zombie socket entries
#     for `dockerInference` and/or `userAnalyticsOtlpHttp.sock`
#   * `ls %LOCALAPPDATA%\docker-secrets-engine\` shows the same for
#     `engine.sock`
#
# Root cause has not been fully nailed down but it correlates with:
#   * Windows reboots (esp. after Windows or Claude updates)
#   * The WSL `docker-desktop` distro getting unregistered
#   * Stale AF_UNIX socket files left in NTFS as broken reparse points that
#     no tool can delete (`fsutil`, `Remove-Item`, `del /f` all return
#     ERROR_CANT_ACCESS_FILE / 1920)
#
# This script applies the proven workaround in ~30 seconds:
#   1. Multi-sweep kill of every Docker / com.docker / docker-* process
#   2. `wsl --shutdown` to release any held handles
#   3. Stash %LOCALAPPDATA%\Docker\run\ and %LOCALAPPDATA%\docker-secrets-engine\
#      aside (atomic rename — the zombie files can't be deleted, but the
#      *parent dir* can be renamed); recreate fresh empty dirs
#   4. If `docker-desktop` distro is gone, re-register it from the
#      preserved ext4.vhdx on D:\
#   5. Launch Docker Desktop and poll for the engine to come up
#   6. (engine-ready confirmation)
#   7. Heal containers whose State.Error matches the Docker-Desktop
#      stale-bind-mount pattern by force-recreating them via their
#      original compose context (auto-discovered from container labels).
#      This addresses a separate failure mode that hits ~immediately
#      AFTER engine recovery: bind-mount-cache misses on containers like
#      otel-collector + neo4j-warmup that mount a single config file.
#
# Safe to run anytime. Idempotent — skips steps that aren't needed.
#
# Usage:
#   Right-click → "Run with PowerShell", OR
#   double-click scripts\fix-docker.bat (which calls this), OR
#   pwsh -NoProfile -File scripts\fix-docker.ps1
#
# Does NOT require admin. The privileged `com.docker.service` is left
# alone (Docker Desktop handles it on launch).
# =============================================================================

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# Require PowerShell 7+. Windows PowerShell 5.1's parser misreads a few
# constructs we rely on (`&&` inside string literals, nested double quotes
# inside `{{...}}` blocks). The .bat wrapper auto-prefers pwsh.exe when
# present, but if someone runs this directly under powershell.exe we want a
# clear "use pwsh" message rather than a wall of parse errors.
if ($PSVersionTable.PSVersion.Major -lt 7) {
    Write-Host 'ERROR: this script requires PowerShell 7+ (pwsh).' -ForegroundColor Red
    Write-Host 'You appear to be running Windows PowerShell 5.1. Install pwsh from' -ForegroundColor Red
    Write-Host '  https://aka.ms/powershell  (or `winget install Microsoft.PowerShell`)' -ForegroundColor Red
    Write-Host 'and re-run via scripts\fix-docker.bat (the wrapper prefers pwsh).' -ForegroundColor Red
    exit 2
}

# --- Config ---------------------------------------------------------------
$DockerDesktopExe = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
$LocalAppData     = $env:LOCALAPPDATA
$RunDir           = Join-Path $LocalAppData 'Docker\run'
$SecretsDir       = Join-Path $LocalAppData 'docker-secrets-engine'
$DistroVhdx       = 'D:\Docker\Storage\DockerDesktopWSL\main\ext4.vhdx'
$EngineWaitSec    = 240   # how long to wait for `docker info` to succeed

# --- Helpers --------------------------------------------------------------
function Write-Step([string]$msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "    $msg" -ForegroundColor Yellow }
function Write-Err([string]$msg)  { Write-Host "    $msg" -ForegroundColor Red }

# Detect zombie socket files inside a dir: stat fails ⇒ broken reparse point.
function Test-DirHasZombies([string]$dir) {
    if (-not (Test-Path $dir)) { return $false }
    foreach ($item in Get-ChildItem -LiteralPath $dir -Force -ErrorAction SilentlyContinue) {
        try { [void]$item.Length } catch { return $true }
    }
    # Also catch case where Get-ChildItem itself can't enumerate due to a broken child:
    # try a wsl `ls` and look for `-?` lines.
    $wslList = & wsl.exe -d Ubuntu ls -la $dir.Replace('C:\','/mnt/c/').Replace('\','/') 2>&1
    if ($wslList -match '^[lcbsp\-]\?{9}\s') { return $true }
    return $false
}

# --- Step 1: kill all Docker procs ---------------------------------------
Write-Step 'Killing Docker Desktop processes (multi-sweep)'
for ($sweep = 1; $sweep -le 4; $sweep++) {
    $procs = Get-Process | Where-Object {
        $_.ProcessName -like 'Docker*' -or
        $_.ProcessName -like 'com.docker*' -or
        $_.ProcessName -like 'docker-*'
    }
    if (-not $procs) { break }
    $procs | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}
$leftover = Get-Process | Where-Object {
    $_.ProcessName -like 'Docker*' -or $_.ProcessName -like 'com.docker*'
}
if ($leftover) {
    Write-Warn "Some Docker processes survived 4 sweeps:"
    $leftover | ForEach-Object { Write-Warn "      $($_.ProcessName) PID=$($_.Id)" }
} else {
    Write-Ok 'all Docker processes terminated'
}

# --- Step 2: WSL shutdown -------------------------------------------------
Write-Step 'wsl --shutdown'
& wsl.exe --shutdown 2>&1 | Out-Null
Start-Sleep -Seconds 2
Write-Ok 'WSL stopped'

# --- Step 3: stash zombie socket dirs ------------------------------------
Write-Step 'Checking for zombie AF_UNIX socket files'
$stashedAny = $false
foreach ($spec in @(
    @{ Dir = $RunDir;     Prefix = 'run' },
    @{ Dir = $SecretsDir; Prefix = 'docker-secrets-engine' }
)) {
    $hasZombies = Test-DirHasZombies $spec.Dir
    if ($hasZombies) {
        $ts = Get-Date -Format 'yyyyMMdd-HHmmss'
        $stashName = "$($spec.Prefix).broken-$ts"
        $stashPath = Join-Path (Split-Path $spec.Dir -Parent) $stashName
        try {
            Rename-Item -LiteralPath $spec.Dir -NewName $stashName -ErrorAction Stop
            New-Item -ItemType Directory -Force -Path $spec.Dir | Out-Null
            Write-Ok "$($spec.Dir): zombies stashed to $stashName, fresh empty dir created"
            $stashedAny = $true
        } catch {
            Write-Err "could not rename $($spec.Dir): $($_.Exception.Message)"
        }
    } else {
        Write-Ok "$($spec.Dir): clean"
    }
}
if (-not $stashedAny) { Write-Ok 'no zombie dirs found' }

# --- Step 4: re-register docker-desktop distro if missing ----------------
Write-Step 'Checking WSL `docker-desktop` distribution'
$wslList = & wsl.exe -l --quiet 2>&1
# wsl --list output is UTF-16; flatten and check
$wslJoined = ($wslList -join '`n').Replace("`0", '')
if ($wslJoined -match 'docker-desktop') {
    Write-Ok '`docker-desktop` distro is registered'
} else {
    Write-Warn '`docker-desktop` distro is missing — re-registering from D:\ VHDX'
    if (-not (Test-Path -LiteralPath $DistroVhdx)) {
        Write-Err "ext4.vhdx not found at $DistroVhdx"
        Write-Err 'Confirm D:\ is mounted and the path exists, then re-run.'
        exit 1
    }
    & wsl.exe --import-in-place docker-desktop $DistroVhdx 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok 'distro re-registered successfully'
    } else {
        Write-Err "wsl --import-in-place failed with exit $LASTEXITCODE"
        exit 1
    }
}

# --- Step 5: launch Docker Desktop ----------------------------------------
Write-Step 'Launching Docker Desktop'
if (-not (Test-Path -LiteralPath $DockerDesktopExe)) {
    Write-Err "Docker Desktop.exe not found at $DockerDesktopExe"
    exit 1
}
Start-Process -FilePath $DockerDesktopExe
Write-Ok 'launched'

# --- Step 6: wait for engine ---------------------------------------------
Write-Step "Waiting up to $EngineWaitSec s for `docker info` to succeed"
$deadline = (Get-Date).AddSeconds($EngineWaitSec)
$ready = $false
while ((Get-Date) -lt $deadline) {
    $null = & docker.exe info 2>&1
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 3
}
if (-not $ready) {
    Write-Err "engine did not become ready within $EngineWaitSec s"
    Write-Err 'Check the Docker Desktop tray icon — it may show a specific error dialog.'
    Write-Err 'Latest backend log:'
    $logPath = Join-Path $LocalAppData 'Docker\log\host\com.docker.backend.exe.log'
    if (Test-Path -LiteralPath $logPath) {
        Get-Content -LiteralPath $logPath -Tail 10 | ForEach-Object { Write-Host "      $_" }
    }
    exit 1
}
Write-Ok ('engine ready at ' + (Get-Date -Format HH:mm:ss))

# --- Step 7: heal containers stuck on stale bind-mount cache --------------
# Docker Desktop caches bind-mount source paths under
#   /run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/Ubuntu/<sha256>
# When Docker Desktop restarts (which we just did) the hashes change but
# container metadata still references the OLD hashes. Containers with
# bind-mounted config files (e.g. otel-collector, neo4j-warmup) crash on
# next start with "not a directory: Are you trying to mount a directory
# onto a file (or vice-versa)?".  Fix: force-recreate so the container is
# created against fresh bind-mount hashes.
Write-Step 'Scanning for containers with stale bind-mount metadata'

$bindMountPattern = 'docker-desktop-bind-mounts/Ubuntu/[a-f0-9]+.*not a directory'
$brokenByProject = @{}

# Build the Go-template format strings by char-code concat. PowerShell 5.1's
# parser doesn't tolerate nested double quotes inside `{{...}}` (even when
# backtick-escaped) — it leaks the string boundary and cascades brace
# errors. Building via [char]34 ('"') avoids any quote-in-quote at all.
$dq = [char]34
$fmtProj  = '{{index .Config.Labels ' + $dq + 'com.docker.compose.project'              + $dq + '}}'
$fmtSvc   = '{{index .Config.Labels ' + $dq + 'com.docker.compose.service'              + $dq + '}}'
$fmtWd    = '{{index .Config.Labels ' + $dq + 'com.docker.compose.project.working_dir'  + $dq + '}}'
$fmtFiles = '{{index .Config.Labels ' + $dq + 'com.docker.compose.project.config_files' + $dq + '}}'

$composedNames = & docker.exe ps -a --filter 'label=com.docker.compose.project' --format '{{.Names}}' 2>&1
foreach ($name in @($composedNames)) {
    $name = "$name".Trim()
    if (-not $name) { continue }
    $errText = & docker.exe inspect --format '{{.State.Error}}' $name 2>&1
    if ("$errText" -match $bindMountPattern) {
        $proj  = (& docker.exe inspect --format $fmtProj  $name 2>&1).Trim()
        $svc   = (& docker.exe inspect --format $fmtSvc   $name 2>&1).Trim()
        $wd    = (& docker.exe inspect --format $fmtWd    $name 2>&1).Trim()
        $files = (& docker.exe inspect --format $fmtFiles $name 2>&1).Trim()
        $key = "$proj`0$wd`0$files"
        if (-not $brokenByProject.ContainsKey($key)) {
            $brokenByProject[$key] = New-Object System.Collections.Generic.List[string]
        }
        $brokenByProject[$key].Add($svc) | Out-Null
        Write-Warn "  $name (service=$svc) — stale bind-mount, will recreate"
    }
}

if ($brokenByProject.Count -eq 0) {
    Write-Ok 'no stale bind-mount containers found'
} else {
    foreach ($key in $brokenByProject.Keys) {
        $parts = $key.Split([char]0)
        $proj  = $parts[0]
        $wd    = $parts[1]
        $files = $parts[2]
        $services = $brokenByProject[$key] | Sort-Object -Unique

        # Build -f args from the comma-separated config_files label.
        # Each file path is already absolute (e.g. /home/georag/.../docker-compose.yml)
        $fileArgs = ($files.Split(',') | Where-Object { $_ } | ForEach-Object { "-f '$_'" }) -join ' '
        $svcArgs = ($services -join ' ')

        # Use `set -e` + semicolons instead of `&&` so the script parses on
        # Windows PowerShell 5.1 (which has a tokenizer bug treating `&&` as
        # a chain operator even inside double-quoted strings).
        $bashCmd = "set -e; cd '$wd'; docker compose -p '$proj' $fileArgs up -d --no-deps --force-recreate $svcArgs"
        Write-Step "Recreating $($services.Count) service(s) in project '$proj'"
        Write-Host "    services: $svcArgs"

        # Run via WSL because the working_dir + config_files are WSL paths
        # (compose-up was originally invoked from inside WSL).
        $out = & wsl.exe -d Ubuntu bash -lc $bashCmd 2>&1
        $out | ForEach-Object { Write-Host "    $_" }
        if ($LASTEXITCODE -ne 0) {
            Write-Err "    compose recreate exited $LASTEXITCODE"
        } else {
            Write-Ok '    recreate completed'
        }
    }
}

# --- Final summary --------------------------------------------------------
$summary = & docker.exe info --format 'server={{.ServerVersion}} containers={{.Containers}} running={{.ContainersRunning}} images={{.Images}}' 2>&1
Write-Host ''
Write-Host '------------------------------------------------------------------'
Write-Host "  $summary" -ForegroundColor Green
Write-Host '------------------------------------------------------------------'
Write-Host ''
Write-Host 'Bring the GeoRAG stack back up with:' -ForegroundColor Cyan
# Built via char-code concat to avoid Windows PowerShell 5.1 parser quirks
# around (a) bare && inside string literals and (b) literal " inside
# single-quoted strings, both of which destabilise the tokenizer.
$amp = [char]38
$qot = [char]34
$cmd = '  wsl -d Ubuntu bash -lc ' + $qot + 'cd /home/georag/projects/georag ' + $amp + $amp + ' docker compose -p georagintelligencev10 -f docker-compose.yml -f docker/compose.langfuse.yml --profile dev-full up -d' + $qot
Write-Host $cmd
exit 0
