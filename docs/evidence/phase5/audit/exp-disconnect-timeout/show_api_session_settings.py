"""Open a connection exactly the way the API does (psycopg_pool.ConnectionPool
with the default ATUS_DATABASE_URL, no extra options; see
src/atus_pipeline/api/app.py:62-68 and dependencies.py:38) and report the
session's effective timeout settings and their source. Read-only."""

from psycopg_pool import ConnectionPool

URL = "postgresql://atus:atus@localhost:5434/atus"  # config.py:16 default / .env.example
NAMES = (
    "statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout",
    "idle_session_timeout", "client_connection_check_interval",
    "tcp_keepalives_idle", "tcp_keepalives_interval", "tcp_keepalives_count",
    "default_transaction_read_only", "max_parallel_workers_per_gather",
)
with ConnectionPool(URL, min_size=1, max_size=1, name="audit-show") as pool:
    with pool.connection(timeout=10) as conn:
        print("current_user =", conn.execute("select current_user").fetchone()[0])
        print("in transaction after first statement:", conn.info.transaction_status.name)
        for name, setting, unit, source in conn.execute(
            "select name, setting, unit, source from pg_settings where name = any(%s) order by name",
            (list(NAMES),),
        ):
            print(f"{name} = {setting}{unit or ''} (source: {source})")
        print("SHOW statement_timeout ->", conn.execute("show statement_timeout").fetchone()[0])
        print("role config:", conn.execute(
            "select rolname, rolconfig, rolsuper from pg_roles where rolname = current_user"
        ).fetchall())
        print("db/role settings:", conn.execute(
            "select coalesce(d.datname,'<all>'), coalesce(r.rolname,'<all>'), s.setconfig "
            "from pg_db_role_setting s left join pg_database d on d.oid = s.setdatabase "
            "left join pg_roles r on r.oid = s.setrole"
        ).fetchall())
        print("database config (pg_database.datconfig not in PG16; see pg_db_role_setting above)")
