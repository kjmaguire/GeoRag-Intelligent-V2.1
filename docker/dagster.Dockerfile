# =============================================================================
# docker/dagster.Dockerfile
#
# Dagster ingestion pipeline image.
#
# IMPORTANT: Dagster runs as TWO separate compose services from this image:
#   - dagster-daemon     → CMD: dagster-daemon run
#                          Handles schedulers, sensors, run launchers.
#                          No exposed port; background process only.
#   - dagster-webserver  → CMD: dagster-webserver -h 0.0.0.0 -p 3001
#                          Pipeline UI on port 3001.
#
# Do NOT attempt to combine these into a single process. The daemon and
# webserver must run independently so the scheduler keeps ticking even when
# no one has the UI open.
#
# Architecture reference: Section 07 (Deployment Services), Section 11b (V1 Scope)
# Orchestration rule: Dagster handles scheduled/bulk data pipelines.
#                     Laravel queues handle user-triggered async work.
#                     Never overlap (CLAUDE.md hard rule 7).
# =============================================================================

FROM python:3.13-slim

LABEL org.opencontainers.image.title="GeoRAG Dagster"
LABEL org.opencontainers.image.description="Dagster ingestion pipeline — shared image for daemon and webserver services"

# ---------------------------------------------------------------------------
# System dependencies
#
# build-essential  → C extensions (asyncpg, shapely, rasterio, segyio)
# libpq-dev        → PostgreSQL client headers (dagster-postgres uses psycopg2)
# gdal-bin         → GDAL tools for geo format parsing
# libgdal-dev      → GDAL C headers (rasterio, fiona, osgeo build deps)
# libgeos-dev      → GEOS (GeoPandas / Shapely)
# libproj-dev      → PROJ (pyproj, rasterio CRS support)
# libhdf5-dev      → HDF5 for Geosoft GDB and grid format parsing
# curl             → healthcheck / utility
# git              → pip editable installs from git sources if needed
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    gdal-bin \
    libgdal-dev \
    libgeos-dev \
    libproj-dev \
    libhdf5-dev \
    curl \
    git \
    tesseract-ocr \
    tesseract-ocr-eng \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# GDAL environment variables (must precede any GDAL Python package install).
ENV GDAL_CONFIG=/usr/bin/gdal-config
ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

# ---------------------------------------------------------------------------
# uv for fast, reproducible dependency installs
# ---------------------------------------------------------------------------
RUN pip install --no-cache-dir uv

# ---------------------------------------------------------------------------
# DAGSTER_HOME — where Dagster stores its local state, run history, and config.
# The compose file should mount a named volume here so run history persists
# across container restarts.
#
# Expected layout inside DAGSTER_HOME:
#   dagster.yaml   — instance config (postgres storage, local artifact storage)
#   storage/       — run history SQLite or Postgres-backed store
# ---------------------------------------------------------------------------
ENV DAGSTER_HOME=/opt/dagster/dagster_home

RUN mkdir -p ${DAGSTER_HOME} \
    && chown -R nobody:nogroup ${DAGSTER_HOME}

WORKDIR /app

# ---------------------------------------------------------------------------
# Dependency installation
#
# Core Dagster packages:
#   dagster              — orchestration framework
#   dagster-postgres     — run/event storage backed by PostgreSQL
#   dagster-docker       — DockerRunLauncher for running ops in containers
#   dagster-webserver    — Dagit UI (renamed package in 1.x)
#
# Geo / science parsing:
#   gdal                 — Python bindings matching the system GDAL version
#   geopandas            — vector geo data (shapefile, geojson, gpkg)
#   fiona                — OGR vector I/O (GeoPandas dependency, listed explicitly)
#   rasterio             — raster geo data (GeoTIFF, netCDF)
#   pyproj               — CRS transformations
#   lasio                — LAS well-log file parsing
#   segyio               — SEG-Y seismic format parsing
#   obspy                — passive seismic / earthquake data parsing
#
# Data processing:
#   polars               — fast DataFrame library (preferred over pandas for ingest)
#   duckdb               — in-process analytical SQL (Bronze→Silver transforms)
#
# We install from pyproject.toml via uv with a lockfile when available,
# otherwise fall back to direct pip install of the package list.
# ---------------------------------------------------------------------------
COPY pyproject.toml ./
COPY uv.lock* ./

# Install all dependencies from pyproject.toml without editable mode.
# Source isn't copied yet, so -r reads [project.dependencies] directly.
RUN uv pip install --system --no-cache -r pyproject.toml \
    || pip install --no-cache-dir $(python3 -c "\
import tomllib, pathlib; \
d = tomllib.loads(pathlib.Path('pyproject.toml').read_text()); \
print(' '.join(d['project']['dependencies']))")

# Copy pipeline code after deps to preserve the dep cache layer.
COPY . .

# Register the package itself (entry points, module resolution).
RUN uv pip install --system --no-cache --no-deps . 2>/dev/null || true

# Run as nobody — Dagster doesn't need root, and DAGSTER_HOME is pre-chowned.
USER nobody

# No EXPOSE here. The webserver port (3001) is declared in docker-compose.yml
# on the dagster-webserver service. The daemon has no network-facing port.

# Default command: run the daemon.
# Override in compose for the webserver service:
#   dagster-webserver: dagster-webserver -h 0.0.0.0 -p 3001
CMD ["dagster-daemon", "run"]
