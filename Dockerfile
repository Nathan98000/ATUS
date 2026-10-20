# ATUS analysis API image (Phase 5): the exact artifact CI scans, pushes to
# GHCR and Render runs by digest (docs/adr/ADR-011-hosting.md).
#
#   docker build -t atus-api:dev --build-arg ATUS_BUILD_SHA="$(git rev-parse HEAD)" \
#     --build-arg ATUS_VERSION="$(python -c 'import atus_pipeline; print(atus_pipeline.__version__)')" .
#   docker buildx build --platform linux/amd64 ...      # what Render runs
#
# Stages:
#   build    hash-checked install of requirements/api.lock.txt into /opt/venv.
#            The project wheel is built with the locked setuptools in a
#            throwaway build venv, so nothing unpinned is ever downloaded.
#   runtime  the pinned slim base plus /opt/venv only: no compilers, build
#            backend, tests, data, docs, frontend or migrations. Runs as an
#            unprivileged user.
#
# Migrations are deliberately not in the image (audit F56). The API never
# changes the schema: production restores a serving dump (pg_restore) and the
# loader runs from a source checkout (`atus migrate` / `atus load`).
#
# Configuration is 12-factor: every setting comes from the environment
# (.env.production.example). The image defaults to ATUS_ENV=production, which
# refuses to start without ATUS_DATABASE_URL and ATUS_EDGE_SECRETS rather than
# falling back to the development database URL (audit F44).

# The multi-arch index digest of python:3.12-slim (3.12.15, Debian trixie),
# so linux/amd64 and linux/arm64 builds both start from a pinned base. Bump
# both values together.
ARG PYTHON_BASE=python:3.12-slim
ARG PYTHON_DIGEST=sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

FROM ${PYTHON_BASE}@${PYTHON_DIGEST} AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_NO_INPUT=1

WORKDIR /build

# Dependencies first, so source edits do not invalidate this layer.
# --only-binary: every pinned package ships wheels for linux/amd64 and
# linux/arm64; refusing sdists keeps compilers out of the build entirely.
COPY requirements/api.lock.txt requirements/build.lock.txt requirements/
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --require-hashes --no-deps --only-binary=:all: \
        -r requirements/api.lock.txt \
 && python -m venv /opt/buildenv \
 && /opt/buildenv/bin/pip install --require-hashes --no-deps --only-binary=:all: \
        -r requirements/build.lock.txt

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

# pip compiles every module to bytecode on install, so the runtime image
# never compiles at start-up. The import check fails the build if the API
# needs a package the lock left out; the curl_cffi check proves the
# pipeline-only dependency stayed out (requirements/api.in), which is also
# why `pip check` in the image reports curl-cffi as missing: expected.
RUN /opt/buildenv/bin/pip wheel --no-deps --no-build-isolation --wheel-dir /dist . \
 && /opt/venv/bin/pip install --no-deps --no-index /dist/atus_pipeline-*.whl \
 && /opt/venv/bin/python -c "import atus_pipeline.api.app, atus_pipeline.cli" \
 && if /opt/venv/bin/python -c "import curl_cffi" 2>/dev/null; then \
        echo "curl_cffi must not be in the API image" >&2; exit 1; \
    fi


FROM ${PYTHON_BASE}@${PYTHON_DIGEST} AS runtime

ARG PYTHON_BASE
ARG PYTHON_DIGEST
ARG ATUS_VERSION=unknown
ARG ATUS_BUILD_SHA=dev

LABEL org.opencontainers.image.title="atus-api" \
      org.opencontainers.image.description="Read-only HTTP API over the American Time Use Survey analytical engine" \
      org.opencontainers.image.source="https://github.com/Nathan98000/ATUS" \
      org.opencontainers.image.url="https://github.com/Nathan98000/ATUS" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${ATUS_VERSION}" \
      org.opencontainers.image.revision="${ATUS_BUILD_SHA}" \
      org.opencontainers.image.base.name="docker.io/library/${PYTHON_BASE}" \
      org.opencontainers.image.base.digest="${PYTHON_DIGEST}"

# A fixed numeric UID/GID so platform "run as non-root" checks can verify it.
RUN groupadd --system --gid 10001 atus \
 && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent \
        --no-create-home --shell /usr/sbin/nologin atus

COPY --from=build /opt/venv /opt/venv

# SSL_CERT_FILE: libpq inside the psycopg[binary] wheel uses its own OpenSSL,
# whose compiled-in certificate directory is not Debian's. Pointing it at the
# base image's CA bundle is what makes `sslrootcert=system` (libpq >= 16)
# verify Neon's public certificate (docs/deployment.md, "TLS to the database").
# PORT: Render sets it (10000); the default matches so a bare `docker run`
# behaves the same. WEB_CONCURRENCY=1: one uvicorn process (ADR-011).
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    ATUS_ENV=production \
    PORT=10000 \
    WEB_CONCURRENCY=1

# An empty, root-owned working directory: no `.env` file can be picked up and
# the app user cannot write anything next to the code.
WORKDIR /app
USER 10001:10001
EXPOSE 10000

# Liveness only: /health never touches the database, so the check cannot wake
# a scale-to-zero DB. Render ignores Docker HEALTHCHECK and probes /health
# itself; this one serves docker compose and local runs.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --start-interval=2s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '10000'), timeout=4).read()"]

# Last, so a new commit only rebuilds this metadata layer.
ENV ATUS_BUILD_SHA=${ATUS_BUILD_SHA}

# `exec` replaces the shell, so uvicorn is PID 1 and receives SIGTERM directly.
# The shell only expands $PORT; the API reads every other setting itself.
CMD ["sh", "-c", "exec atus api --host 0.0.0.0 --port \"${PORT}\""]
