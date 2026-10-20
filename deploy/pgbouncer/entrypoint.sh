#!/bin/sh
# Render pgbouncer.ini and its auth file from the environment, then run pgbouncer.
#
# Defaults are Neon's published pooler settings (https://neon.com/docs/connect/
# connection-pooling, read 2026-10-20): pool_mode=transaction,
# max_client_conn=10000, default_pool_size=0.9 * max_connections,
# max_prepared_statements=1000, query_wait_timeout=120. Set
# PGBOUNCER_MAX_PREPARED_STATEMENTS=0 to emulate a pooler without
# protocol-level prepared statement support.
#
# Users and passwords: PGBOUNCER_USERS="name:password name:password"
# (URL-safe passwords; the stack generates them). The auth file lives in a
# private temporary directory and is never printed.
set -eu

: "${PGBOUNCER_UPSTREAM_HOST:=db}"
: "${PGBOUNCER_UPSTREAM_PORT:=5432}"
: "${PGBOUNCER_POOL_MODE:=transaction}"
: "${PGBOUNCER_MAX_CLIENT_CONN:=10000}"
# 0.9 x max_connections (839 for the 0.25-2 CU compute; see neon_settings.py).
: "${PGBOUNCER_DEFAULT_POOL_SIZE:=755}"
: "${PGBOUNCER_MAX_PREPARED_STATEMENTS:=1000}"
: "${PGBOUNCER_QUERY_WAIT_TIMEOUT:=120}"
: "${PGBOUNCER_USERS:?set PGBOUNCER_USERS to space-separated name:password pairs}"

umask 077
conf_dir=$(mktemp -d /tmp/pgbouncer.XXXXXX)

for pair in $PGBOUNCER_USERS; do
  name=${pair%%:*}
  password=${pair#*:}
  printf '"%s" "%s"\n' "$name" "$password"
done > "$conf_dir/userlist.txt"

cat > "$conf_dir/pgbouncer.ini" <<EOF
[databases]
* = host=$PGBOUNCER_UPSTREAM_HOST port=$PGBOUNCER_UPSTREAM_PORT

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
unix_socket_dir =
auth_type = scram-sha-256
auth_file = $conf_dir/userlist.txt
pool_mode = $PGBOUNCER_POOL_MODE
max_client_conn = $PGBOUNCER_MAX_CLIENT_CONN
default_pool_size = $PGBOUNCER_DEFAULT_POOL_SIZE
max_prepared_statements = $PGBOUNCER_MAX_PREPARED_STATEMENTS
query_wait_timeout = $PGBOUNCER_QUERY_WAIT_TIMEOUT
ignore_startup_parameters = extra_float_digits
; the health check connects every 2 s; keep the log readable
log_connections = 0
log_disconnections = 0
EOF

echo "pgbouncer: pool_mode=$PGBOUNCER_POOL_MODE max_prepared_statements=$PGBOUNCER_MAX_PREPARED_STATEMENTS upstream=$PGBOUNCER_UPSTREAM_HOST:$PGBOUNCER_UPSTREAM_PORT"
exec pgbouncer "$conf_dir/pgbouncer.ini"
