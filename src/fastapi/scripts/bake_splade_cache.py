"""Build-time helper: download SPLADE++ weights into the image's HF cache.

Invoked once by docker/fastapi.Dockerfile during the builder stage — not a
runtime script. Keep the model identity here in sync with
app/services/sparse_encoder.py (SPARSE_MODEL_NAME / SPARSE_MODEL_REVISION).
"""

from __future__ import annotations

from transformers import AutoModelForMaskedLM, AutoTokenizer

_NAME = "naver/splade-cocondenser-ensembledistil"
_REVISION = "49cf4c7b0db5b870a401ddf5e2669993ef3699c7"

if __name__ == "__main__":
    AutoTokenizer.from_pretrained(
        _NAME, revision=_REVISION, trust_remote_code=False, cache_dir="/opt/hf_cache",
    )
    AutoModelForMaskedLM.from_pretrained(
        _NAME, revision=_REVISION, trust_remote_code=False, cache_dir="/opt/hf_cache",
    )
