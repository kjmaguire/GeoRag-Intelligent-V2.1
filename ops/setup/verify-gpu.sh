#!/usr/bin/env bash
# GeoRAG GPU Passthrough Verification Script
# Run this INSIDE WSL Ubuntu AFTER running fix-gpu-passthrough.sh
# AND after running 'wsl --shutdown' + restarting WSL.

set -e

PASS=0
FAIL=0

check() {
    local label="$1"
    local cmd="$2"
    local expect="$3"
    echo -n "  CHECK: $label ... "
    local result
    result=$(eval "$cmd" 2>&1) || true
    if echo "$result" | grep -q "$expect"; then
        echo "PASS"
        PASS=$((PASS+1))
    else
        echo "FAIL"
        echo "    Output: $result" | head -5
        FAIL=$((FAIL+1))
    fi
}

echo ""
echo "=== GeoRAG GPU Passthrough Verification ==="
echo ""

echo "--- Kernel & WSL ---"
check "WSL2 kernel version >= 5.10.43" "uname -r" "microsoft"
check "Ubuntu 24.04" "cat /etc/os-release" "24.04"

echo ""
echo "--- GPU Device Nodes ---"
check "/dev/dxg exists" "ls /dev/dxg" "dxg"

echo ""
echo "--- NVIDIA WSL Libs ---"
check "/usr/lib/wsl/lib/libcuda.so exists" "ls /usr/lib/wsl/lib/libcuda.so" "libcuda"
check "/usr/lib/wsl/lib in ld.so.conf" "cat /etc/ld.so.conf.d/ld.wsl.conf" "/usr/lib/wsl/lib"

echo ""
echo "--- NVIDIA Container Toolkit ---"
check "nvidia-container-toolkit package installed" "dpkg -l nvidia-container-toolkit" "ii"
check "nvidia-container-cli binary exists" "which nvidia-container-cli" "nvidia-container-cli"
check "nvidia-container-runtime binary exists" "which nvidia-container-runtime" "nvidia-container-runtime"
check "config.toml exists" "ls /etc/nvidia-container-runtime/config.toml" "config.toml"

echo ""
echo "--- nvidia-smi ---"
echo -n "  CHECK: nvidia-smi runs without NVML error ... "
SMI_OUTPUT=$(nvidia-smi 2>&1) || true
if echo "$SMI_OUTPUT" | grep -q "RTX 4080\|GeForce RTX"; then
    echo "PASS"
    echo "    Driver version: $(echo "$SMI_OUTPUT" | grep "Driver Version" | awk '{print $3}')"
    PASS=$((PASS+1))
elif echo "$SMI_OUTPUT" | grep -q "blocked\|NVML"; then
    echo "FAIL -- WSL GPU service needs restart"
    echo "    Run from elevated PowerShell: wsl --shutdown"
    FAIL=$((FAIL+1))
else
    echo "FAIL"
    echo "    $SMI_OUTPUT" | head -5
    FAIL=$((FAIL+1))
fi

echo ""
echo "--- Docker Runtime ---"
check "Docker nvidia runtime registered" "docker info" "nvidia"

echo ""
echo "--- Docker GPU Container Test ---"
echo -n "  CHECK: docker run --gpus all nvidia-smi works ... "
DOCKER_GPU=$(docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu24.04 nvidia-smi 2>&1) || true
if echo "$DOCKER_GPU" | grep -q "RTX 4080\|GeForce RTX\|Driver Version"; then
    echo "PASS"
    echo "    $(echo "$DOCKER_GPU" | grep "Driver Version")"
    PASS=$((PASS+1))
else
    echo "FAIL"
    echo "    $DOCKER_GPU" | head -8
    FAIL=$((FAIL+1))
fi

echo ""
echo "--- Ollama GPU Test (if running) ---"
echo -n "  CHECK: Ollama container GPU flag ... "
if docker ps 2>/dev/null | grep -q ollama; then
    OLLAMA_GPU=$(docker inspect ollama 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); caps=d[0].get('HostConfig',{}).get('DeviceRequests',[]); print('GPU' if caps else 'NO_GPU')" 2>/dev/null) || true
    if [ "$OLLAMA_GPU" = "GPU" ]; then
        echo "PASS (GPU device request found)"
        PASS=$((PASS+1))
    else
        echo "FAIL (Ollama running but no GPU device request)"
        FAIL=$((FAIL+1))
    fi
else
    echo "SKIP (Ollama not running -- start with: docker compose --profile dev-llm up ollama)"
    PASS=$((PASS+1))
fi

echo ""
echo "==================================="
echo "Results: $PASS passed, $FAIL failed"
if [ $FAIL -eq 0 ]; then
    echo "GPU passthrough is fully working."
    echo "You can now run: docker compose --profile dev-llm up ollama"
else
    echo "Some checks failed. See above for details."
    exit 1
fi
echo "==================================="
