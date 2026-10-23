"""Read-only footprint measurement of an ATUS database.

usage: python measure.py <database_url> [label]
Runs every statement in a READ ONLY transaction.
"""
import sys
import psycopg

url = sys.argv[1]
label = sys.argv[2] if len(sys.argv) > 2 else url
with psycopg.connect(url, options="-c default_transaction_read_only=on") as conn:
    conn.execute("SET TRANSACTION READ ONLY")
    print(f"# {label}")
    print("server_version:", conn.execute("show server_version").fetchone()[0])
    print("transaction_read_only:", conn.execute("show transaction_read_only").fetchone()[0])
    rows = conn.execute("""
        SELECT c.relname,
               pg_total_relation_size(c.oid) AS total,
               pg_relation_size(c.oid) AS heap,
               pg_indexes_size(c.oid) AS idx,
               coalesce(pg_total_relation_size(c.reltoastrelid),0) AS toast
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'atus' AND c.relkind IN ('r','p')
        ORDER BY 2 DESC
    """).fetchall()
    tot = 0
    print(f"{'table':32s} {'rows':>12s} {'total_MB':>10s} {'heap_MB':>9s} {'idx_MB':>8s} {'toast_MB':>8s}")
    for name, total, heap, idx, toast in rows:
        n = conn.execute(f"SELECT count(*) FROM atus.{name}").fetchone()[0]
        tot += total
        print(f"{name:32s} {n:12,d} {total/2**20:10.1f} {heap/2**20:9.1f} {idx/2**20:8.1f} {toast/2**20:8.2f}")
    print(f"{'SUM atus user objects':32s} {'':12s} {tot/2**20:10.1f}")
    other = conn.execute("""
        SELECT n.nspname, c.relname, pg_total_relation_size(c.oid)
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname NOT IN ('atus','pg_catalog','information_schema','pg_toast')
          AND c.relkind IN ('r','p','m')
    """).fetchall()
    for nsp, rel, sz in other:
        print(f"other user table {nsp}.{rel}: {sz/2**20:.3f} MB")
    db = conn.execute("SELECT current_database(), pg_database_size(current_database())").fetchone()
    print(f"pg_database_size('{db[0]}') = {db[1]:,d} bytes = {db[1]/2**20:.1f} MB")
    for dn, sz in conn.execute("SELECT datname, pg_database_size(datname) FROM pg_database ORDER BY 1"):
        print(f"  db {dn:24s} {sz/2**20:10.1f} MB")
    s = int(conn.execute("SELECT sum(pg_database_size(datname)) FROM pg_database").fetchone()[0])
    print(f"sum(pg_database_size(datname)) over cluster = {s:,d} bytes = {s/2**20:.1f} MB")
