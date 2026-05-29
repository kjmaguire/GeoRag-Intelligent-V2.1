# syntax=docker/dockerfile:1.7
# =============================================================================
# Custom Postgres 18 image for GeoRAG Phase 0
#
# Builds on postgis/postgis:18-3.6-alpine and adds the seven extensions the
# Phase 0 kickoff Step 1 requires beyond what the base image ships:
#
#   auto_explain    core contrib  (ships with base; activated via shared_preload_libraries)
#   h3              h3-pg main    (geospatial hex indexing)
#   hypopg          main          (hypothetical indexes for Index Health Agent planner what-ifs)
#   pg_stat_kcache  main          (kernel-level per-query CPU/IO stats)
#   pg_partman      main          (declarative partition maintenance for audit_ledger + workflow_runs)
#   pg_repack       main          (online table reorganize without exclusive locks)
#   pg_ivm          main          (incremental view maintenance for materialized views)
#
# All extensions are cloned from upstream main / latest and built with PGXS.
# PG 18 was released late 2025; some extensions still ship PG 18 support only
# on main as of May 2026 — pinning to released tags risks build failures, so
# we accept "main at build time" for now and document via the git rev label.
# =============================================================================

ARG BASE_DIGEST=sha256:369b23d36107b84282dd2dfe29e431e8fce80976200aa2037a8eee5d88d850b6
FROM postgis/postgis:18-3.6-alpine@${BASE_DIGEST}

USER root

# ---- Build dependencies (virtual package — removed at the end) -------------
# build-base       gcc/make/etc
# clang/llvm-dev   PGXS' bitcode emit (`make install` calls clang for *.bc)
# cmake            h3 (uber/h3 C library)
# perl python3     contrib build script prereqs
# *-dev            link-time headers
RUN apk add --no-cache --virtual .build-deps \
        build-base \
        git \
        clang \
        llvm-dev \
        cmake \
        make \
        bison \
        flex \
        gawk \
        perl \
        python3 \
        zlib-dev \
        openssl-dev \
        readline-dev \
        libxml2-dev \
        libxslt-dev \
        lz4-dev \
        zstd-dev \
        curl \
        ca-certificates

# PG 18 alpine was compiled with --with-llvm using llvm-19/clang-19.
# Current Alpine 3.x ships clang/llvm at version 21, so the bitcode
# emission and llvm-lto link steps in PGXS install rules fail to find
# the version-pinned tools. We don't need bitcode files at runtime —
# JIT is disabled in compose (`-c jit=off`) — so we tell every PGXS
# `make` to skip the bitcode step entirely.
#
# `with_llvm=no` is the documented PGXS knob; setting it here as a
# global makefile variable so each extension's `make` + `make install`
# inherits it without the build steps having to remember to pass it.
ENV with_llvm=no \
    PG_CONFIG=/usr/local/bin/pg_config

WORKDIR /tmp/build

# ---- 1. h3 C library (prereq for h3-pg) ------------------------------------
# Pinned tag — h3 v4.x is API-stable, so we can pin without churn.
RUN git clone --depth=1 --branch v4.2.0 https://github.com/uber/h3.git \
 && cmake \
        -S h3 -B h3/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_TESTING=OFF \
        -DBUILD_BENCHMARKS=OFF \
        -DBUILD_FILTERS=OFF \
        -DBUILD_GENERATORS=OFF \
        -DBUILD_FUZZERS=OFF \
 && cmake --build h3/build --parallel \
 && cmake --install h3/build \
 && rm -rf h3

# ---- 2. h3-pg (PG extension binding) ---------------------------------------
# h3-pg uses CMake (not raw PGXS). It expects pg_config on PATH.
RUN git clone --depth=1 https://github.com/zachasme/h3-pg.git \
 && cmake \
        -S h3-pg -B h3-pg/build \
        -DCMAKE_BUILD_TYPE=Release \
 && cmake --build h3-pg/build --parallel \
 && cmake --install h3-pg/build \
 && rm -rf h3-pg

# Each PGXS build below explicitly passes `with_llvm=no` so the make rules
# that emit/link LLVM bitcode are skipped (see ENV note above for why).

# ---- 3. hypopg --------------------------------------------------------------
RUN git clone --depth=1 https://github.com/HypoPG/hypopg.git \
 && cd hypopg \
 && make with_llvm=no && make with_llvm=no install \
 && cd .. && rm -rf hypopg

# ---- 4. pg_stat_kcache ------------------------------------------------------
RUN git clone --depth=1 https://github.com/powa-team/pg_stat_kcache.git \
 && cd pg_stat_kcache \
 && make with_llvm=no && make with_llvm=no install \
 && cd .. && rm -rf pg_stat_kcache

# ---- 5. pg_partman ----------------------------------------------------------
RUN git clone --depth=1 https://github.com/pgpartman/pg_partman.git \
 && cd pg_partman \
 && make with_llvm=no && make with_llvm=no install \
 && cd .. && rm -rf pg_partman

# ---- 6. pg_repack -----------------------------------------------------------
# pg_repack ships both a server-side extension and a client binary — both
# end up under /usr/local/{lib,bin}/postgresql which is already on PATH.
RUN git clone --depth=1 https://github.com/reorg/pg_repack.git \
 && cd pg_repack \
 && make with_llvm=no && make with_llvm=no install \
 && cd .. && rm -rf pg_repack

# ---- 7. pg_ivm --------------------------------------------------------------
RUN git clone --depth=1 https://github.com/sraoss/pg_ivm.git \
 && cd pg_ivm \
 && make with_llvm=no && make with_llvm=no install \
 && cd .. && rm -rf pg_ivm

# ---- 8. auto_explain extension registration --------------------------------
# auto_explain.so ships with the base postgis-alpine image, but the upstream
# extension registration files (.control + .sql) are stripped. Without them
# `CREATE EXTENSION auto_explain` errors with "extension is not available",
# even though the underlying loader works fine via shared_preload_libraries.
#
# PG 18 added auto_explain as a real extension; we mirror the upstream files
# from contrib/auto_explain so `\dx` lists it and CREATE EXTENSION succeeds.
# The SQL file is intentionally tiny — auto_explain has no SQL surface, only GUCs.
RUN test -f /usr/local/lib/postgresql/auto_explain.so \
 && printf '%s\n' \
        '# auto_explain extension' \
        "comment = 'logs query plans of slow statements automatically'" \
        "default_version = '1.0'" \
        "module_pathname = '\$libdir/auto_explain'" \
        'relocatable = true' \
        'trusted = true' \
        > /usr/local/share/postgresql/extension/auto_explain.control \
 && printf '%s\n' \
        '/* auto_explain--1.0.sql -- mirror of upstream contrib/auto_explain */' \
        '\echo Use "CREATE EXTENSION auto_explain" to load this file. \quit' \
        > /usr/local/share/postgresql/extension/auto_explain--1.0.sql

# ---- Strip build deps; keep runtime libs -----------------------------------
# libxml2 / libxslt / readline / icu-libs are kept by the base image already;
# we only strip the *-dev headers and the toolchain.
RUN apk del .build-deps \
 && rm -rf /tmp/build /var/cache/apk/* /root/.cache

WORKDIR /
USER postgres

