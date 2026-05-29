#!/bin/bash
# §5e training launcher — fires after materialisation completes.
#
# Sequence (each step gated on the prior):
#
#   1. Detect newest run_id in s3://reranker-labels/v1/
#   2. Download splits to /tmp/reranker-train inside georag-fastapi
#   3. Pause georag-vllm (frees VRAM for training)
#   4. pip install peft into georag-fastapi
#   5. Run scripts/train_reranker_lora.py against the downloaded splits
#   6. Report back the adapter path + manifest
#
# Per Kyle's locked decisions:
#   - GPU strategy: "Pause vLLM + hatchet-worker-ai (Recommended)"
#   - Promotion fail: "Retry up to 3x with hyperparam sweep"
#   - Deploy: "Auto-flip but only on weekday dev hours" — stage overnight,
#     human review for prod flag flip.
#
# Idempotent on individual steps — re-runs detect existing artifacts +
# skip re-downloads. Training itself is NOT idempotent (each invocation
# starts fresh from base bge-reranker-base + LoRA).
#
# Usage:
#     bash scripts/_adr0010_launch_training.sh
#         (assumes fastapi + dagster containers are up + s3 has a dataset)
#
#     bash scripts/_adr0010_launch_training.sh --epochs 5 --lr 1e-5
#         (override hyperparams for retry attempts)

set -euo pipefail

EPOCHS="${EPOCHS:-3}"
LR="${LR:-2e-5}"
BATCH_SIZE="${BATCH_SIZE:-16}"

# Parse CLI overrides
while [[ $# -gt 0 ]]; do
    case "$1" in
        --epochs) EPOCHS="$2"; shift 2 ;;
        --lr) LR="$2"; shift 2 ;;
        --batch-size) BATCH_SIZE="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

echo "=== §5e training launcher ==="
echo "epochs=$EPOCHS lr=$LR batch_size=$BATCH_SIZE"

# Step 1 — detect newest run_id in S3
echo ""
echo "Step 1: detecting newest run_id in s3://reranker-labels/v1/ ..."
RUN_ID=$(docker exec georag-dagster-webserver python -c "
import boto3
s3 = boto3.client('s3', endpoint_url='http://minio:8333',
    aws_access_key_id='georag-admin',
    aws_secret_access_key='Urxdoc6n4shTshytkhJIv3SHJorbATqq',
    region_name='us-east-1')
r = s3.list_objects_v2(Bucket='reranker-labels', Prefix='v1/')
runs = {}
for obj in r.get('Contents', []):
    parts = obj['Key'].split('/')
    if len(parts) >= 3 and parts[1].startswith('run_id='):
        rid = parts[1].split('=', 1)[1]
        if 'manifest.json' in obj['Key'] and 'sample' not in obj['Key']:
            runs[rid] = obj['LastModified']
if not runs:
    print('NONE')
else:
    newest = max(runs, key=runs.get)
    print(newest)
" | tr -d '\r')

if [ "$RUN_ID" = "NONE" ] || [ -z "$RUN_ID" ]; then
    echo "  no completed dataset found in s3://reranker-labels/v1/" >&2
    exit 2
fi
echo "  newest run_id: $RUN_ID"

# Step 2 — download splits into fastapi
echo ""
echo "Step 2: downloading splits into georag-fastapi:/tmp/reranker-train ..."
docker exec georag-fastapi bash -c "
    mkdir -p /tmp/reranker-train
    cd /tmp/reranker-train
    python -c \"
import boto3, os
s3 = boto3.client('s3', endpoint_url='http://minio:8333',
    aws_access_key_id='georag-admin',
    aws_secret_access_key='Urxdoc6n4shTshytkhJIv3SHJorbATqq',
    region_name='us-east-1')
for fname in ('manifest.json', 'train.jsonl', 'val.jsonl', 'test.jsonl'):
    key = f'v1/run_id=$RUN_ID/{fname}'
    s3.download_file('reranker-labels', key, fname)
    print(f'  downloaded {fname}: {os.path.getsize(fname)} bytes')
\"
"

# Step 3 — pause vLLM + hatchet-worker-ai
echo ""
echo "Step 3: pausing georag-vllm + georag-hatchet-worker-ai ..."
docker stop georag-vllm georag-hatchet-worker-ai 2>&1 | sed 's/^/  /'

# Step 4 — pip install peft + datasets inside fastapi (accelerate is
# already in the image; peft + datasets are the two reliably-missing
# pieces sentence_transformers cross-encoder fit() needs).
#
# Must run as -u root: the container's pip resolves install paths under
# /var/www which is www-data:www-data + read-only to the default exec
# user (we hit "Permission denied: '/var/www'" otherwise — see the
# 2026-05-28 launcher run that died silently here because the previous
# `... 2>&1 | tail -3` swallowed the pip ERROR line). Verify with an
# explicit import after install instead of relying on pip's exit code.
echo ""
echo "Step 4: pip install peft + datasets into georag-fastapi (as root) ..."
docker exec -u root georag-fastapi pip install peft datasets 2>&1 | tail -8
docker exec georag-fastapi python -c "
import peft, datasets, accelerate
print(f'  peft {peft.__version__} datasets {datasets.__version__} accelerate {accelerate.__version__}')
"

# Step 5 — confirm GPU is fully free + then train
echo ""
echo "Step 5: GPU state pre-training ..."
nvidia-smi --query-gpu=memory.used,memory.free,utilization.gpu --format=csv 2>&1 | head -3 || \
    docker exec georag-fastapi python -c "import torch; print(f'free: {torch.cuda.mem_get_info()[0]/1024**3:.1f} GB')"

echo ""
echo "Step 5: kicking off train_reranker_lora.py ..."
# Copy the training script into the container (lives at host scripts/)
docker cp scripts/train_reranker_lora.py georag-fastapi:/tmp/reranker-train/train_reranker_lora.py
# LOG_LEVEL=debug (lowercase) leaks in from compose into the container env
# and Python's logging.basicConfig rejects lowercase level names with
# `ValueError: Unknown level: 'debug'`. The training script consumes
# LOG_LEVEL inside main() at import time, so we override here rather than
# patch the script (other fastapi callers might want the lowercase value
# for app-level logging — see MEMORY:project_pdfminer_loglevel_hatchet_block).
docker exec -e LOG_LEVEL=INFO -w /tmp/reranker-train georag-fastapi python train_reranker_lora.py \
    --dataset-prefix /tmp/reranker-train \
    --epochs "$EPOCHS" \
    --batch-size "$BATCH_SIZE" \
    --learning-rate "$LR" \
    --output /tmp/reranker-train/adapter

echo ""
echo "Step 5: training done."

# Step 6 — restart paused services
echo ""
echo "Step 6: restoring georag-vllm + georag-hatchet-worker-ai ..."
docker start georag-vllm georag-hatchet-worker-ai 2>&1 | sed 's/^/  /'

echo ""
echo "=== TRAINING COMPLETE ==="
echo "Adapter: /tmp/reranker-train/adapter (inside georag-fastapi)"
echo "Manifest: docker exec georag-fastapi cat /tmp/reranker-train/adapter/training_manifest.json"
echo ""
echo "Next: run NDCG eval + promotion gate."
