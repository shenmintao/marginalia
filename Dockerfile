# syntax=docker/dockerfile:1.7
# Multi-stage build for Marginalia. The api and worker share one image —
# the entrypoint dispatches based on the `command:` set in compose.

ARG PYTHON_VERSION=3.12
# Mirror endpoints are arg-driven so an upstream environment that's not
# in mainland China can switch them off with `--build-arg APT_MIRROR= ...`.
ARG APT_MIRROR=http://mirrors.tuna.tsinghua.edu.cn
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

FROM python:${PYTHON_VERSION}-slim AS builder

ARG APT_MIRROR
ARG PIP_INDEX_URL

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONDONTWRITEBYTECODE=1

# Swap Debian's default repo for a domestic mirror, then install build
# deps. Most Python packages publish manylinux wheels — build-essential
# is here only as a fallback for any sdist that slips through. `git` is
# required because pyproject.toml pulls glowpy from a git+https URL.
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|http://deb.debian.org|${APT_MIRROR}|g; s|http://security.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources 2>/dev/null \
         || sed -i "s|http://deb.debian.org|${APT_MIRROR}|g; s|http://security.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list ; \
    fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends build-essential git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

# Install into an isolated prefix we then copy into the runtime stage.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install .


FROM python:${PYTHON_VERSION}-slim AS runtime

ARG APT_MIRROR

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    MARGINALIA_HOME=/data

# Runtime libs only — no compilers. libmagic helps content-type sniffing
# in some upload paths; pypdfium2 needs no system lib (statically linked).
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|http://deb.debian.org|${APT_MIRROR}|g; s|http://security.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources 2>/dev/null \
         || sed -i "s|http://deb.debian.org|${APT_MIRROR}|g; s|http://security.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list ; \
    fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libmagic1 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY alembic.ini /app/alembic.ini
COPY alembic /app/alembic

WORKDIR /app

# /data is the on-disk footprint root. Compose mounts a named volume here
# so the mirror vault, sqlite (if used), and object pool survive restarts.
RUN mkdir -p /data && useradd --system --uid 10001 marginalia \
 && chown -R marginalia:marginalia /data /app
USER marginalia

EXPOSE 8000

# Schema bootstrap runs on app startup (marginalia.db.bootstrap), so the
# container just needs to exec uvicorn. Worker service overrides command
# to `marginalia-worker`.
#
# SECURITY: this binds 0.0.0.0 *inside the container*. If you publish the
# port beyond loopback (e.g. `docker run -p 8000:8000`, or editing the
# compose file's `127.0.0.1:8000:8000` bind for LAN exposure), you MUST
# set MARGINALIA_API_TOKEN — without it every endpoint is unauthenticated.
# MARGINALIA_API_HOST mirrors the uvicorn --host flag below so the app's
# startup checks (the unauthenticated-bind warning) see the real bind
# address rather than the 127.0.0.1 default.
ENV MARGINALIA_API_HOST=0.0.0.0
CMD ["uvicorn", "marginalia.main:app", "--host", "0.0.0.0", "--port", "8000"]
