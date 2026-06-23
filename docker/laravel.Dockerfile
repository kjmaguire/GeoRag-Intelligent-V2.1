# =============================================================================
# docker/laravel.Dockerfile
#
# Multi-stage build for the Laravel 13 application running on Octane + Swoole.
#
# IMPORTANT: This single Dockerfile is shared by three compose services that
# use different CMD overrides:
#   - laravel-octane  → php artisan octane:start --host=0.0.0.0 --port=80 --server=swoole
#   - laravel-horizon → php artisan horizon
#   - laravel-reverb  → php artisan reverb:start --host=0.0.0.0 --port=8080
#
# Do NOT use php-fpm here. Octane keeps the application in memory for
# performance. The app boots once; php-fpm's per-request boot model is the
# wrong paradigm entirely.
#
# Architecture reference: Section 07 (Deployment Services)
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1 — builder
# Installs all build-time dependencies, Composer packages, and Node assets.
# Nothing from this stage bloats the final image except the outputs we COPY.
# -----------------------------------------------------------------------------
# 2026-06-03 sweep: digest captured from `docker pull php:8.5-cli`.
# Re-pin via the same after a PHP 8.5.x patch. Both builder + runtime
# stages MUST use the same digest so the PECL extensions compiled in
# builder match the runtime PHP ABI byte-for-byte.
FROM php:8.5-cli@sha256:1954ff5cd21f222c992b79d25e403b2600cec829678d5bb7076883f3a44c0d6e AS builder

# Build-time system dependencies.
# libpq-dev      → pdo_pgsql / pgsql extensions
# libzip-dev     → zip extension
# libpng-dev     → GD (PNG support)
# libjpeg-dev    → GD (JPEG support)
# libfreetype6-dev → GD (font rendering)
# unixodbc-dev   → ODBC (future connectors)
# curl, git, zip, unzip → Composer and general tooling
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    libzip-dev \
    libpng-dev \
    libjpeg-dev \
    libfreetype6-dev \
    unixodbc-dev \
    curl \
    git \
    zip \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Configure and install PHP extensions.
# GD needs explicit --with flags for JPEG and Freetype support.
# NOTE: opcache is statically built into php:8.5-cli (visible in
# `php -m` under [Zend Modules]); explicit install fails with
# "cp: cannot stat 'modules/*'" because the build produces no
# shared .so. The opcache-production.ini below tunes it in place.
RUN docker-php-ext-configure gd \
        --with-freetype \
        --with-jpeg \
    && docker-php-ext-install \
        pdo_pgsql \
        pgsql \
        zip \
        gd \
        pcntl \
        sockets \
        bcmath

# Install Swoole via PECL.
# Swoole is the async server that powers Octane. We enable:
#   --enable-swoole-pgsql   → async PostgreSQL client built into Swoole
#   --enable-openssl        → TLS support for WebSocket / HTTP2
# Note: PECL Swoole build flags are passed via INI-style prompt responses.
#
# 2026-06-03 sweep: PECL versions pinned. Previously `pecl install swoole
# redis` grabbed whatever was latest at build time — a surprise Swoole
# bump can break Octane in subtle ways (worker lifecycle, signal handling,
# coroutine semantics). Bump deliberately:
#   - swoole 6.2.0+ adds PHP 8.5 support; 6.x is in Octane 2's tested band
#   - phpredis 6.3.0 includes the PHP 8.5 compile fix
RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/* \
    && pecl install swoole-6.2.1 redis-6.3.0 \
    && docker-php-ext-enable swoole redis

# Install Composer from its official image — avoids curling an installer script.
# 2026-06-03 sweep: digest captured from `docker pull composer:2`.
COPY --from=composer:2@sha256:7725eb4545c438629ae8bde3ef0bb9a5038ef566126ad878442a69007242d267 /usr/bin/composer /usr/bin/composer

# Install Node.js 22.x (LTS) for the Inertia SSR + Vite asset build.
# We pin the major version; the NodeSource script locks to the latest 22.x patch.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install PHP dependencies first — layer cached until composer.lock changes.
COPY composer.json composer.lock ./
RUN composer install \
        --no-dev \
        --no-interaction \
        --no-scripts \
        --optimize-autoloader \
        --prefer-dist

# Install Node dependencies — layer cached until package-lock.json changes.
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts

# Copy the full application source now that deps are installed.
COPY . .

# Run Composer post-install scripts that were skipped above (e.g. package:discover).
# We do this after the full source is present so autoload maps are complete.
RUN composer run-script post-autoload-dump --no-interaction || true

# Build the production frontend assets (Vite + Inertia SSR).
RUN npm run build

# -----------------------------------------------------------------------------
# Stage 2 — runtime
# Lean image that contains only what is needed to run the application.
# We re-install system packages and PHP extensions from scratch rather than
# copying from builder; this keeps the runtime image clean and auditable.
# -----------------------------------------------------------------------------
FROM php:8.5-cli@sha256:1954ff5cd21f222c992b79d25e403b2600cec829678d5bb7076883f3a44c0d6e AS runtime

LABEL org.opencontainers.image.title="GeoRAG Laravel"
LABEL org.opencontainers.image.description="Laravel 13 on Octane/Swoole — shared image for octane, horizon, reverb services"

# Runtime system dependencies (same set as builder, minus build-only tools).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    libzip-dev \
    libpng-dev \
    libjpeg-dev \
    libfreetype6-dev \
    unixodbc-dev \
    libssl-dev \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# PHP extensions — identical to builder stage.
# NOTE: opcache is statically built into php:8.5-cli (see builder stage above).
# 2026-06-03 sweep: PECL versions pinned in lockstep with the builder
# stage above (swoole-6.2.1 / redis-6.3.0). Both stages MUST move
# together — version skew between builder + runtime PECL extensions
# silently produces a runtime image whose extension ABI doesn't match
# the build that compiled it.
RUN docker-php-ext-configure gd \
        --with-freetype \
        --with-jpeg \
    && docker-php-ext-install \
        pdo_pgsql \
        pgsql \
        zip \
        gd \
        pcntl \
        sockets \
        bcmath \
    && pecl install swoole-6.2.1 redis-6.3.0 \
    && docker-php-ext-enable swoole redis

# OPcache tuning for Octane (preload-friendly, long TTL since code doesn't change at runtime).
# validate_timestamps=0 is safe because we rebuild the image on deploy.
RUN { \
    echo "opcache.enable=1"; \
    echo "opcache.memory_consumption=256"; \
    echo "opcache.interned_strings_buffer=16"; \
    echo "opcache.max_accelerated_files=20000"; \
    echo "opcache.validate_timestamps=0"; \
    echo "opcache.save_comments=1"; \
    echo "opcache.fast_shutdown=1"; \
} > /usr/local/etc/php/conf.d/opcache-production.ini

# Increase PHP memory limit — Octane keeps the app in memory; 512M is safe for
# a dev workstation with 64 GB RAM. Adjust downward if running many replicas.
RUN echo "memory_limit=512M" > /usr/local/etc/php/conf.d/memory.ini

WORKDIR /app

# Copy application (vendor, built assets, and source) from builder.
COPY --from=builder /app /app

# Create required runtime directories with correct ownership.
# storage/ and bootstrap/cache/ must be writable by the www-data equivalent.
RUN mkdir -p \
        storage/logs \
        storage/framework/cache \
        storage/framework/sessions \
        storage/framework/views \
        bootstrap/cache \
    && chown -R www-data:www-data storage bootstrap/cache \
    && chmod -R 775 storage bootstrap/cache

# Octane listens on port 80 (HTTP).
# Reverb WebSocket port (8080) is defined as an exposed port in compose —
# we expose it here as documentation; the compose service applies the binding.
EXPOSE 80
EXPOSE 8080

# Health check for the Octane process.
# Laravel's /up route is provided out-of-the-box in Laravel 11+.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:80/up || exit 1

# Default command starts the Octane/Swoole HTTP server.
# Override this in docker-compose.yml for horizon and reverb containers:
#   horizon:  php artisan horizon
#   reverb:   php artisan reverb:start --host=0.0.0.0 --port=8080
CMD ["php", "artisan", "octane:start", "--host=0.0.0.0", "--port=80", "--server=swoole"]
