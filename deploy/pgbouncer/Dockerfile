# PgBouncer in transaction mode for the emulation profile
# (docker-compose.emulation.yml, profile `pgbouncer`; brief §10).
#
# Built on the same pinned postgres:16 base as the stack database, installing
# pgbouncer from that image's signed apt repositories (apt.postgresql.org /
# Debian) instead of a third-party image. The configuration is rendered from
# the environment at start (entrypoint.sh) because secrets must not be baked
# into the image.
ARG POSTGRES_BASE=postgres:16
ARG POSTGRES_DIGEST=sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94

FROM ${POSTGRES_BASE}@${POSTGRES_DIGEST}

RUN apt-get update \
 && apt-get install -y --no-install-recommends pgbouncer \
 && rm -rf /var/lib/apt/lists/*

COPY --chmod=0755 entrypoint.sh /usr/local/bin/pgbouncer-entrypoint

# pgbouncer refuses to run as root.
USER postgres
EXPOSE 6432
HEALTHCHECK --interval=2s --timeout=3s --retries=30 \
    CMD ["pg_isready", "-h", "127.0.0.1", "-p", "6432"]
ENTRYPOINT ["/usr/local/bin/pgbouncer-entrypoint"]
