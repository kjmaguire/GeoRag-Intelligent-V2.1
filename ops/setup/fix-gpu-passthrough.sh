#!/usr/bin/env bash
# GeoRAG GPU Passthrough Fix Script
# Run this INSIDE WSL Ubuntu with: bash fix-gpu-passthrough.sh
# You will be prompted for your sudo password once at the start.
#
# Diagnosis summary:
#   - Windows driver: 32.0.15.9597 (= NVIDIA 559.97) -- GOOD, above 535 minimum
#   - WSL2 kernel: 6.6.87.2-microsoft-standard-WSL2 -- GOOD, above 5.10.43 minimum
#   - WSL version: 2.6.3.0 -- GOOD
#   - /dev/dxg: EXISTS (crw-rw-rw-) -- GOOD, GPU device node is present
#   - /usr/lib/wsl/lib/: EXISTS with nvidia libs -- GOOD, Windows driver side is fine
#   - /usr/lib/wsl/lib/ in ld.so.conf: YES (via /etc/ld.so.conf.d/ld.wsl.conf) -- GOOD
#   - nvidia-container-toolkit: NOT INSTALLED -- ROOT CAUSE #1
#   - /etc/nvidia-container-runtime/config.toml: MISSING -- ROOT CAUSE #2
#   - Docker NVIDIA runtime: registered in Docker Desktop but toolkit binaries absent
#
# The error "nvidia-container-cli: initialization error: WSL environment detected
# but no adapters were found" is caused by the Container Toolkit being absent.
# Docker Desktop registers the nvidia runtime name, but the actual CLI binary
# (nvidia-container-cli) is not installed, so it falls back to a broken legacy path.

set -e

echo "==> Step 1: Caching sudo credentials..."
sudo -v

echo ""
echo "==> Step 2: Adding NVIDIA Container Toolkit GPG key..."
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

echo ""
echo "==> Step 3: Adding NVIDIA Container Toolkit apt repository..."
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

echo ""
echo "==> Step 4: Updating apt package lists..."
sudo apt-get update -qq

echo ""
echo "==> Step 5: Installing nvidia-container-toolkit..."
sudo apt-get install -y nvidia-container-toolkit

echo ""
echo "==> Step 6: Configuring Container Toolkit for Docker runtime..."
# This writes /etc/nvidia-container-runtime/config.toml with:
#   - no-cgroups = false
#   - ldconfig = @/sbin/ldconfig.real (important for WSL2)
#   The --cdi-enabled flag is needed for Docker Desktop WSL2 mode
sudo nvidia-ctk runtime configure --runtime=docker

echo ""
echo "==> Step 7: Patching config.toml for WSL2 environment..."
# Docker Desktop on WSL2 uses its own Docker daemon; the config.toml needs
# 'no-cgroups = false' (already default) and the ldconfig path must be correct.
# Check if /sbin/ldconfig.real exists (Ubuntu-specific) vs /sbin/ldconfig
if [ -f /sbin/ldconfig.real ]; then
    echo "    ldconfig.real found at /sbin/ldconfig.real -- correct for Ubuntu"
else
    echo "    WARNING: /sbin/ldconfig.real not found. Checking ldconfig..."
    ls -la /sbin/ldconfig* 2>/dev/null || true
fi

# Show the generated config
echo ""
echo "    Generated /etc/nvidia-container-runtime/config.toml:"
cat /etc/nvidia-container-runtime/config.toml

echo ""
echo "==> Step 8: Verifying nvidia-container-cli is now installed..."
which nvidia-container-cli && nvidia-container-cli --version

echo ""
echo "==> Step 9: Testing NVML access directly..."
nvidia-smi || {
    echo ""
    echo "    nvidia-smi still failing. This can happen if the WSL GPU service"
    echo "    needs a restart. See Step 10 for the fix."
}

echo ""
echo "==> Step 10: Instructions for restarting the WSL GPU service..."
echo "    nvidia-smi will report 'GPU access blocked by the operating system'"
echo "    until the lxss-related services are refreshed. The safest fix is:"
echo ""
echo "    FROM A WINDOWS POWERSHELL (run as Administrator):"
echo "       Get-Service LxssManager | Restart-Service"
echo "    OR:"
echo "       wsl --shutdown"
echo "       # Wait 5-10 seconds, then:"
echo "       wsl -d Ubuntu"
echo ""
echo "    After shutdown+restart, re-run this script's verification:"
echo "       nvidia-smi"
echo "       docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu24.04 nvidia-smi"
echo ""

echo "==> Step 11: Verifying Docker daemon knows about the NVIDIA runtime config..."
# Docker Desktop manages its own daemon.json -- we should NOT write to
# /etc/docker/daemon.json in WSL when using Docker Desktop, as Docker Desktop
# writes its own config and merges it. The nvidia runtime is already registered
# (confirmed: 'docker info' shows 'Runtimes: nvidia runc io.containerd.runc.v2').
# The missing piece was only the toolkit binaries, which are now installed.
echo "    Current Docker runtimes:"
docker info 2>/dev/null | grep -E "Runtimes|Default Runtime" || echo "    (Docker not reachable from this shell -- normal if running before toolkit restart)"

echo ""
echo "==================================================================="
echo "SUMMARY OF FIXES APPLIED:"
echo "  [OK] NVIDIA Container Toolkit GPG key added"
echo "  [OK] NVIDIA Container Toolkit apt repo added"
echo "  [OK] nvidia-container-toolkit package installed"
echo "  [OK] /etc/nvidia-container-runtime/config.toml generated"
echo ""
echo "REQUIRED MANUAL STEP (cannot be automated):"
echo "  Run in an ELEVATED Windows PowerShell:"
echo "    wsl --shutdown"
echo "  Then reopen WSL and test:"
echo "    nvidia-smi"
echo "    docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu24.04 nvidia-smi"
echo "==================================================================="
