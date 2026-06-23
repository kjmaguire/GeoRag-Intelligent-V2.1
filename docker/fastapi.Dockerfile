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
FROM python:3.13-slim@sha256:c33f0bc4364a6881bed1ec0cc2665e6c53c87a43e774aaeab88e6f17af105e4f AS tesseract-builder

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
FROM python:3.13-slim@sha256:c33f0bc4364a6881bed1ec0cc2665e6c53c87a43e774aaeab88e6f17af105e4f AS builder

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

# Copy dependency manifest first to maximise Docker layer caching.
# The heavy "install all deps" layer only re-runs when pyproject.toml changes.
COPY pyproject.toml ./
COPY uv.lock* ./

# Install all project dependencies into the system Python (no virtualenv —
# simpler single-env model inside containers). Falls back to plain pip if
# uv cannot parse pyproject.toml (e.g. missing uv.lock on first run).
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
RUN uv pip install --system --no-cache -r pyproject.toml \
    || ( python3 -c "\
import tomllib, pathlib; \
deps = tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['dependencies']; \
pathlib.Path('/tmp/reqs.txt').write_text('\n'.join(deps) + '\n')" \
    && pip install --no-cache-dir -r /tmp/reqs.txt )

# Dev tools — pytest + pytest-asyncio. Image carries them so test runs
# work after a fresh `docker compose up -d --force-recreate fastapi`
# instead of requiring a manual `pip install pytest` post-recreate.
# Production deploys can strip these by adding a separate runtime stage
# that omits the dev install; for now they're <5 MB so not worth the
# extra Dockerfile complexity.
RUN pip install --no-cache-dir pytest>=8.0 pytest-asyncio>=0.25

# FastAPI review #9 — slowapi for the optional rate limiter. Dormant
# unless RATE_LIMIT_ENABLED=true; baked in so flipping the flag at
# runtime doesn't require a rebuild.
RUN pip install --no-cache-dir slowapi>=0.1.9

# Copy application source and register the package itself (entry points etc.).
# --no-deps avoids re-installing already-present transitive deps.
COPY . .
RUN uv pip install --system --no-cache --no-deps . 2>/dev/null || true


# =============================================================================
# Stage 2 — runtime
# Lean image: runtime shared libraries only, no compiler toolchain.
# =============================================================================
FROM python:3.13-slim@sha256:c33f0bc4364a6881bed1ec0cc2665e6c53c87a43e774aaeab88e6f17af105e4f AS runtime

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
# Doc-phase 122-fix — OpenCV system libs for paddleocr (which transitively
# loads cv2):
#   libgl1            → libGL.so.1 (OpenGL ABI cv2 links against)
#   libglib2.0-0      → GLib runtime (cv2 + paddleocr fontconfig hooks)
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
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
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
    libgl1 \
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
