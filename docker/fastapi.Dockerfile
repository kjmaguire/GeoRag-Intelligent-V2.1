# =============================================================================
# docker/fastapi.Dockerfile
#
# FastAPI domain service — the Python brain of GeoRAG.
#
# Responsibilities:
#   - RAG pipeline execution (retrieval, reranking, LLM orchestration)
#   - Geo-spatial query processing (PostGIS, Qdrant vector search, Neo4j graph)
#   - Pydantic AI typed output with mandatory citations
#   - Async-native throughout: asyncpg, aioredis, async Qdrant/Neo4j clients
#
# IMPORTANT async rule (from CLAUDE.md hard rules):
#   asyncpg for PostgreSQL, redis.asyncio for Redis, async Qdrant client,
#   async Neo4j driver. Synchronous drivers in async handlers are a
#   blocker-level bug.
#
# Architecture reference: Section 07 (Deployment Services)
#
# Multi-stage build strategy
# --------------------------
# Stage 1 (builder): full -dev headers + build-essential compile all C
#   extensions (asyncpg, cryptography, shapely bindings, GDAL Python wrappers).
# Stage 2 (runtime): only runtime shared libraries — no compiler, no headers.
#   Site-packages and binaries are copied from builder, keeping the final
#   image lean (target < 1.5 GB compressed).
# =============================================================================

# =============================================================================
# Stage 0 — tesseract-builder (2026-06-23 sweep)
# =============================================================================
# Compile Tesseract 5.5 from source. Debian trixie's apt-shipped
# tesseract-ocr caps at 5.4.x; the 5.5.x line ships speed + layout
# improvements that benefit Stage-5 fallback OCR (per ADR-0017).
# Source build is gated to this stage — runtime image only receives
# the resulting /opt/tesseract binaries + tessdata, NOT the toolchain.
#
# To bump: change TESSERACT_VERSION below + rebuild + run
# ops/validation/ocr_cpu_smoke.py against a golden NI 43-101 crop to
# confirm no confidence-distribution regression.
FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a AS tesseract-builder

ARG TESSERACT_VERSION=5.5.2

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        autoconf \
        automake \
        libtool \
        pkg-config \
        libleptonica-dev \
        libpng-dev \
        libjpeg62-turbo-dev \
        libtiff-dev \
        zlib1g-dev \
        libicu-dev \
        libpango1.0-dev \
        libcairo2-dev \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://github.com/tesseract-ocr/tesseract/archive/refs/tags/${TESSERACT_VERSION}.tar.gz" \
        | tar xz -C /tmp \
    && cd "/tmp/tesseract-${TESSERACT_VERSION}" \
    && ./autogen.sh \
    && ./configure --prefix=/opt/tesseract --disable-debug --disable-doc --disable-graphics \
    && make -j"$(nproc)" \
    && make install \
    && rm -rf "/tmp/tesseract-${TESSERACT_VERSION}"

# English language data (fast variant — smaller weights, comparable accuracy
# on printed text in NI 43-101 reports). Bump traineddata source to the
# full LSTM variant under `tessdata` (not `tessdata_fast`) if accuracy
# on degraded scans becomes the bottleneck.
RUN mkdir -p /opt/tesseract/share/tessdata \
    && curl -fsSL https://github.com/tesseract-ocr/tessdata_fast/raw/main/eng.traineddata \
        -o /opt/tesseract/share/tessdata/eng.traineddata


# =============================================================================
# Stage 1 — builder
# Compile every C extension against full -dev headers.
# Nothing from this layer ends up in the final image except site-packages.
# =============================================================================
# 2026-06-03 sweep: digest captured from `docker pull python:3.13-slim`.
# Re-pin via the same after a Python patch release (3.13.x bumps the
# slim base periodically). Both builder + runtime stages MUST use the
# same digest so site-packages copied across stages have matching ABI.
FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a AS builder

# ---------------------------------------------------------------------------
# Build-time system dependencies
#
# build-essential  → GCC/G++/make for C extension compilation
# libpq-dev        → PostgreSQL client headers (asyncpg C layer)
# libgdal-dev      → GDAL C headers (Python gdal/osgeo bindings)
# libgeos-dev      → GEOS geometry engine (Shapely, GeoPandas)
# libproj-dev      → PROJ projections (pyproj, rasterio)
# gdal-bin         → provides gdal-config binary needed by Python GDAL at
#                    build time (gdal-config --version, --cflags, --libs)
# libffi-dev       → cffi compiles against this; WeasyPrint loads Pango /
#                    Cairo via cffi at runtime, but cffi itself needs the
#                    headers at install time. (Doc-phase 122 / §7.9.)
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libffi-dev \
    gdal-bin \
    libgdal-dev \
    libgeos-dev \
    libproj-dev \
    && rm -rf /var/lib/apt/lists/*

# GDAL env vars must be present during Python package compilation so that
# setuptools and pip can locate the correct headers and config binary.
ENV GDAL_CONFIG=/usr/bin/gdal-config
ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

# uv — fast Python package manager used for dependency installation.
RUN pip install --no-cache-dir uv

WORKDIR /app

# Storage-abstraction plan PR1 — build context widened to ./src (see
# docker-compose.yml) so this sibling path dependency is reachable.
# Copied to /georag_object_storage so the relative path in
# src/fastapi/pyproject.toml's [tool.uv.sources] ("../georag_object_storage",
# relative to /app/pyproject.toml) resolves the same way here as it does
# in the host checkout.
COPY georag_object_storage /georag_object_storage

# Geology format parsers (CSV/XLSX/spatial/raster/LAS/XYZ). Same reachability
# and path-resolution story as georag_object_storage above.
COPY georag_geoparsers /georag_geoparsers

# Copy dependency manifest first to maximise Docker layer caching.
# The heavy "install all deps" layer only re-runs when pyproject.toml changes.
COPY fastapi/pyproject.toml ./
COPY fastapi/uv.lock* ./
# The LOCKED dependency set, exported from uv.lock. This is what actually
# gets installed below — see the long note at the main install step for
# why the image no longer resolves pyproject.toml's ranges itself.
COPY fastapi/requirements.lock.txt ./

# Install the local path dependencies FIRST, explicitly. They are path
# entries under [tool.uv.sources], so their bare names in
# project.dependencies would otherwise be resolved against PyPI, where
# neither exists. They are excluded from requirements.lock.txt for the
# same reason (uv exports them as `../georag_object_storage`, a path that
# does not exist in this build context), so these two steps are the only
# thing that puts them in the image —
# scripts/check_fastapi_lock_export.py asserts that stays true.
#
# No `|| pip install` fallback: see the main install step.
RUN uv pip install --system --no-cache /georag_object_storage

RUN uv pip install --system --no-cache /georag_geoparsers

# Install all project dependencies into the system Python (no virtualenv —
# simpler single-env model inside containers), from the exported lock.
# There is no fallback: see the note at the install step itself.
#
# Doc-phase 122: the install now also pulls the `langgraph` optional
# extra by name. The §7 / §8 / §9 / §12 graphs all need LangGraph in
# the runtime image; opt-in via --extra langgraph keeps the install
# story consistent across consumers (Dagster, dev sandboxes can pick).
# 2026-06-23 deps-rot fix complete. The historical workaround here
# (a second `uv pip install` that hardcoded `langgraph>=0.2.50,<0.3`)
# silently DOWNGRADED langgraph from pyproject's `>=1.0.10,<2.0` pin
# after every clean install — the runtime image was shipping
# langgraph 0.2.76 while every "tested on langgraph 1.x" claim
# assumed otherwise. The overlay also installed three other packages
# (langgraph-checkpoint-postgres, langchain-mcp-adapters, langfuse)
# that turned out to be entirely unused — grep across src/fastapi
# returns zero imports for any of them. Removed in lockstep.
#
# Three pyproject changes unblocked the removal:
#   1. pydantic-ai -> pydantic-ai-slim[anthropic,openai]   (6df4726)
#      Closes the meta-package's transitive [bedrock] dep that
#      conflicted with aioboto3>=13.0.
#   2. Top-level llvmlite>=0.43 + numba>=0.60 pins.        (next commit)
#      Stops the resolver backtracking through shap -> numba ->
#      llvmlite==0.36.0 (a 2021 release with no Py 3.13 wheel).
#   3. transformers<5.0 cap stays — optimum-onnx (transitive via
#      sentence-transformers[onnx]) hard-caps it there, so the
#      audit's "lift the cap" finding is moot.
# Pip fallback: emit deps to a requirements.txt file (one per line) rather
# than space-joining into a shell command. PEP 508 markers like
# `onnxruntime-gpu>=1.20; platform_system == 'Linux'` contain semicolons
# and equals that get mangled when the shell re-tokenizes a space-joined
# string. Writing to a file preserves each marker intact for `pip -r`.
# ---------------------------------------------------------------------------
# CPU-only torch, installed BEFORE the main resolve (2026-08-19).
#
# `torch` on PyPI is the CUDA build. torch 2.13.0's linux metadata pulls
# cuda-bindings + cuda-toolkit[cublas,cudart,cufft,...] and triton, measured at
# 3.4 GB inside the running container (nvidia/ 2,724 MB + triton/ 690 MB of a
# 5,899 MB site-packages), and it reports itself as `2.13.0+cu130`. Nothing in
# the Azure deployment has a GPU: the whole inference path is Azure AI Foundry
# (Cohere Embed v4 / Rerank v4 / Command A+), and pyproject's own torch entry
# already says "CPU-only build sufficient -- FastAPI container has no GPU
# passthrough". Nothing enforced it until now.
#
# torch cannot simply be dropped. app/services/sparse_encoder.py loads SPLADE++
# via transformers' AutoModelForMaskedLM, and per CLAUDE.md "SPLADE++ sparse
# retrieval has no Foundry equivalent and stays self-hosted either way".
#
# Installing the +cpu wheel first leaves torch already satisfied for anything
# downstream that depends on it. Belt and braces since 2026-08-22: torch is
# also EXCLUDED from requirements.lock.txt outright
# (scripts/check_fastapi_lock_export.py, EXCLUDED), because the lock resolves
# torch from PyPI where the default wheel is the CUDA build — so leaving a
# `torch==` line in that file would give a future resolver permission to
# reinstall ~3.4 GB of CUDA onto a tier with no GPU. That checker also asserts
# ARG TORCH_VERSION below still matches the version in uv.lock, which is the
# only remaining link between the two now that the export omits it.
#
# Verified in a clean python:3.13-slim container: after both steps torch
# reports 2.13.0+cpu with zero nvidia/triton/cuda- packages.
#
# TORCH_VERSION must be kept in step with pyproject's `torch>=2.13,<3.0` pin.
# If the reranker LoRA fine-tune (src/fastapi/scripts/train_reranker_lora.py,
# currently parked) is ever resumed on a GPU host, restore the CUDA build in
# that environment only:
#   uv pip install --system --reinstall-package torch "torch==2.13.0"
# ---------------------------------------------------------------------------
ARG TORCH_VERSION=2.13.0
RUN uv pip install --system --no-cache \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch==${TORCH_VERSION}" \
    || pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch==${TORCH_VERSION}"

# ---------------------------------------------------------------------------
# THE LOCKED INSTALL
#
# This used to be `uv pip install --system -r pyproject.toml`, with a
# python3/tomllib fallback that re-derived the same ranges for plain pip.
# Both re-RESOLVED pyproject.toml's version ranges at build time. uv.lock
# was copied into the image one layer above and never read.
#
# CI installs with `uv sync --extra dev`, which is uv's PROJECT interface
# and does honour the lock. So CI proved one set of versions worked and
# this image shipped a different set, resolved minutes later, and nothing
# ever compared the two. Any release published inside a range between the
# two resolutions reached production untested.
#
# requirements.lock.txt is exported from uv.lock and committed;
# scripts/check_fastapi_lock_export.py regenerates and diffs it in CI, so
# it cannot drift silently. Regenerate it after any dependency change:
#     python scripts/check_fastapi_lock_export.py --write
#
# NO `|| pip install` FALLBACK, HERE OR ABOVE. The point of installing
# from a lock is that the build gets exactly these versions. A fallback
# that quietly re-resolves on failure reintroduces the whole bug at the
# worst possible moment — when something is already wrong — and turns a
# loud, fixable build failure into a silent version change. If this step
# fails, the build should stop.
# ---------------------------------------------------------------------------

# Dev tools — pytest + pytest-asyncio. Image carries them so test runs
# work after a fresh `docker compose up -d --force-recreate fastapi`
# instead of requiring a manual `pip install pytest` post-recreate.
# Production deploys can strip these by adding a separate runtime stage
# that omits the dev install; for now they're <5 MB so not worth the
# extra Dockerfile complexity.
#
# Deliberately installed BEFORE the locked set, not after. These are the
# only unpinned installs left in the image, and pytest drags in shared
# transitive packages (pluggy, packaging, iniconfig). Running them first
# means the locked install below gets the last word on every shared
# version; running them after would let a dev tool quietly upgrade a
# runtime dependency out from under the lock.
RUN pip install --no-cache-dir "pytest>=8.0" "pytest-asyncio>=0.25"

# ...and now the locked set, which is authoritative over everything above.
RUN uv pip install --system --no-cache -r requirements.lock.txt

# slowapi (FastAPI review #9, the optional rate limiter, dormant unless
# RATE_LIMIT_ENABLED=true) used to be installed here as a separate
# unpinned `pip install slowapi>=0.1.9`. It is a declared project
# dependency, so it is now in requirements.lock.txt at a pinned version
# like everything else and the extra step was doing nothing.

# Copy application source and register the package itself (entry points etc.).
# --no-deps avoids re-installing already-present transitive deps.
COPY fastapi/ .
# `|| true` removed 2026-08-22: this registers the application's own entry
# points, and swallowing its failure produced an image that looked built
# but had no console scripts. --no-deps keeps it from touching the locked
# set installed above.
RUN uv pip install --system --no-cache --no-deps .

# ---------------------------------------------------------------------------
# Bake the SPLADE++ sparse-encoder weights into the image (2026-08-04).
#
# app/services/sparse_encoder.py has no Foundry equivalent — CLAUDE.md:
# "SPLADE++ sparse retrieval has no Foundry equivalent and stays self-hosted
# either way" — so every deployment topology loads this model locally via
# AutoModelForMaskedLM.from_pretrained(), with no cache_dir override, reading
# whatever HF_HOME/TRANSFORMERS_CACHE point at.
#
# On Azure Container Apps that env var points at /tmp/hf_cache, which is
# NOT baked into the image and has no persistent volume behind it — every
# fresh replica (a manual redeploy, a scale-out event, or the nightly
# shutdown-scheduler's restart) starts with an empty cache and must
# re-download the ~440 MB model from HuggingFace Hub before it can serve a
# single real query. With UVICORN_WORKERS processes each racing to do this
# independently, and Azure egress bandwidth to contend with, this was
# observed live taking anywhere from ~2s (occasionally already warm) to
# >2 minutes (a `python -c "from app.services.sparse_encoder import
# encode_sparse; encode_sparse('x')"` call inside the running container hung
# past a 2-minute cap) — well past any query-level timeout budget, and the
# reason chat answers were completing with empty citations rather than
# erroring: search_documents timed out mid-encode and returned zero chunks.
#
# Baking to /opt (not /tmp/hf_cache) mirrors this file's own Tesseract
# convention above and sidesteps relying on /tmp surviving into the runtime
# container. The deployed Container Apps env vars must point HF_HOME /
# TRANSFORMERS_CACHE at this same path for the bake to actually be read.
#
# A standalone script rather than an inline `python -c` — ACR's dependency
# scanner chokes on a multi-line backslash-continued python -c inside a RUN
# instruction ("unable to understand line ...: exit status 1").
RUN python3 scripts/bake_splade_cache.py


# =============================================================================
# Stage 2 — runtime
# Lean image: runtime shared libraries only, no compiler toolchain.
# =============================================================================
FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a AS runtime

LABEL org.opencontainers.image.title="GeoRAG FastAPI"
LABEL org.opencontainers.image.description="FastAPI 0.135.x domain service on Python 3.13"

# ---------------------------------------------------------------------------
# Runtime system dependencies — shared libraries only, no -dev packages
#
# libpq5           → PostgreSQL client runtime (asyncpg loads this .so)
# gdal-bin         → ogr2ogr, gdalinfo CLI tools used at runtime for
#                    geo-format conversion; also pulls in libgdal runtime
# libgdal36        → GDAL shared library (Debian trixie package name)
# libgeos-c1t64    → GEOS geometry runtime (Shapely, GeoPandas)
# libproj25        → PROJ cartographic projection runtime (pyproj)
# curl             → Docker HEALTHCHECK probe
# poppler-utils    → PDF tooling (used by pdfminer.six / pdfplumber)
#
# 2026-06-23 sweep — Tesseract 5.5 from source (ADR-0017):
# Removed trixie's `tesseract-ocr` + `tesseract-ocr-eng` apt packages
# (they cap at 5.4.x). Tesseract 5.5.2 binaries now copied from the
# tesseract-builder stage below; runtime needs the matching shared
# libraries to dynamically link:
#   libleptonica6     → Leptonica image-processing runtime (linked by tesseract)
#   libpng16-16       → PNG runtime
#   libjpeg62-turbo   → JPEG runtime
#   libtiff6          → TIFF runtime
#   libicu76          → ICU runtime (Tesseract's Unicode support)
# Pango / Cairo / GLib are already in the runtime list for WeasyPrint
# so Tesseract gets those for free.
#
# libgl1 removed 2026-08-20. It was here for paddleocr's transitive cv2
# (Doc-phase 122-fix); paddlepaddle/paddleocr were dropped 2026-07-29 and
# cv2 is not installed at all any more. Verified against the running image:
# `apt-cache rdepends --installed libgl1` lists no dependents, so this was
# pulling the OpenGL ABI into a container that never opens a GL context.
# libglib2.0-0 stays — the same comment blamed it on paddleocr, but it is
# genuinely required by the Pango/HarfBuzz/gdk-pixbuf stack below.
#
# Doc-phase 122 / §7.9 — WeasyPrint runtime libraries:
#   libpango-1.0-0       → Pango text layout engine (core WeasyPrint dep)
#   libpangoft2-1.0-0    → Pango + FreeType glyph rendering
#   libcairo2            → Cairo 2D graphics (raster + vector output)
#   libgdk-pixbuf-2.0-0  → image decoder Pango calls into
#   libharfbuzz0b        → text shaping (transitive but explicit for safety)
#   libffi8              → cffi runtime (WeasyPrint binds Pango via cffi)
#   shared-mime-info     → file-type detection for image embedding
#   fonts-liberation     → Liberation Sans/Serif/Mono (matches Arial/Times metrics)
#   fonts-dejavu-core    → DejaVu fallbacks for non-ASCII glyph coverage
#
# postgresql-client-17   → pg_dump, for the backup_postgres workflow. It was
#   absent, so that workflow died on "FileNotFoundError: [Errno 2] No such
#   file or directory: pg_dump" on every scheduled run — nightly, silently,
#   for as long as the logs go back. libpq5 alone gives the client LIBRARY
#   that psycopg and asyncpg link against; it does not give the binaries.
#   Pinned to 17 to stay in step with the server major.
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    postgresql-client-17 \
    gdal-bin \
    libgdal36 \
    libgeos-c1t64 \
    libproj25 \
    curl \
    poppler-utils \
    libleptonica6 \
    libpng16-16 \
    libjpeg62-turbo \
    libtiff6 \
    libicu76 \
    libgomp1 \
    libglib2.0-0 \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libharfbuzz0b \
    libffi8 \
    shared-mime-info \
    fonts-liberation \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Tesseract 5.5 from the tesseract-builder stage (ADR-0017)
# ---------------------------------------------------------------------------
COPY --from=tesseract-builder /opt/tesseract /opt/tesseract
ENV PATH=/opt/tesseract/bin:$PATH \
    TESSDATA_PREFIX=/opt/tesseract/share/tessdata

# ---------------------------------------------------------------------------
# Baked SPLADE++ weights from the builder stage (2026-08-04, see the RUN
# step above for why).
#
# Gotcha found live, after the first deploy of this fix still re-downloaded
# on every request: `from_pretrained(cache_dir='/opt/hf_cache')` at build
# time writes files directly to /opt/hf_cache/models--org--name/... , but
# huggingface_hub does NOT read HF_HOME as that path directly — it derives
# the actual cache root as f"{HF_HOME}/hub" (HF_HUB_CACHE's real default).
# Setting only HF_HOME=/opt/hf_cache made the runtime look in
# /opt/hf_cache/hub/models--..., which doesn't exist, so it silently fell
# through to a fresh network download every time regardless of the bake.
# HF_HUB_CACHE set explicitly bypasses that derivation and points straight
# at where the bake step actually wrote the files.
# ---------------------------------------------------------------------------
COPY --from=builder /opt/hf_cache /opt/hf_cache
ENV HF_HOME=/opt/hf_cache \
    HF_HUB_CACHE=/opt/hf_cache \
    TRANSFORMERS_CACHE=/opt/hf_cache

# ---------------------------------------------------------------------------
# Copy compiled Python environment from builder.
#
# site-packages  → all installed packages (including C extension .so files)
# /usr/local/bin → uvicorn, uv, and other package-installed entry points
#
# We do NOT copy the builder's system libraries (under /usr/lib, /usr/include)
# because the runtime stage installs its own matching .so files above via apt.
# ---------------------------------------------------------------------------
COPY --from=builder /usr/local/lib/python3.13/site-packages \
                    /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin \
                    /usr/local/bin

# Copy application source (needed for module imports and static assets).
WORKDIR /app
COPY --from=builder /app /app

# Non-root user for security. www-data already exists in the slim base image.
RUN chown -R www-data:www-data /app
USER www-data

# FastAPI listens on port 8000.
EXPOSE 8000

# Liveness probe — FastAPI must expose GET /health returning 200.
# The /ready endpoint (readiness — databases connected) is checked by
# docker-compose depends_on, not the Docker daemon healthcheck.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# 4 Uvicorn workers on the dev workstation (8-core Ryzen).
# Each worker is a separate OS process — no GIL contention for CPU-bound
# geo ops. Bump to 8 workers on a production server with more cores.
#
# FastAPI review flags:
#   --no-access-log
#       Disables uvicorn's text access log; replaced by the structured
#       JSON access log emitted by `app.middleware.StructuredAccessLogMiddleware`
#       which is Loki-friendly + carries X-Request-ID.
#   --proxy-headers
#       Honour X-Forwarded-For / X-Forwarded-Proto from a reverse proxy.
#       Without this, request.client.host is always the proxy IP, not the
#       real client — kills any IP-based rate limit or forensic logging.
#   --forwarded-allow-ips '*'
#       Accept proxy headers from any source. In prod, narrow this to
#       your reverse proxy's subnet (e.g. '10.0.0.0/8') so a direct
#       client can't spoof X-Forwarded-For.
#   --timeout-graceful-shutdown 30
#       Wait up to 30 s for in-flight requests to finish before SIGKILL.
#       Default is 5 s which truncates SSE chat streams mid-message on
#       `docker compose stop` — users see "Connection reset" partway
#       through their answer.
#   --header "server:GeoRAG"
#       Replace the default `Server: uvicorn` response header. Minor
#       info-leak fix; also useful operationally so curl/log lines
#       identify the service rather than the framework.
# Hardware-refresh 2026-05-08: --workers is env-driven via UVICORN_WORKERS
# so the dev workstation (Threadripper Pro 5955WX, 16C/32T) can run 6
# workers without rebuilding the image, while staging / prod can pick
# different values. Default 6 — chosen so Postgres parallel workers
# (max_parallel_workers=12), Ollama offload threads (QWEN3_NUM_THREAD=12),
# and the FastAPI uvicorn pool fit inside 32 logical cores without
# starving each other. Drop to 4 on smaller hardware. Shell form is
# required so ${UVICORN_WORKERS:-6} expands at runtime.
CMD uvicorn app.main:app \
        --host 0.0.0.0 \
        --port 8000 \
        --workers ${UVICORN_WORKERS:-6} \
        --no-access-log \
        --proxy-headers \
        --forwarded-allow-ips '*' \
        --timeout-graceful-shutdown 30 \
        --header "server:GeoRAG"
