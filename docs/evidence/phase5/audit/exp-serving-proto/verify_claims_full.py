"""Read-only verification of the brief's section 2 claims on the FULL database.

usage: python verify_claims_full.py <full_db_url>
Every statement runs in one REPEATABLE READ, READ ONLY transaction.
"""
import sys
import time
import psycopg

url = sys.argv[1]
R = [f"tufnwgtp{i:03d}" for i in range(1, 161)]
P = [f"tu20fwgt{i:03d}" for i in range(1, 161)]


def q(conn, label, sql):
    t = time.perf_counter()
    row = conn.execute(sql).fetchone()
    print(f"{label}: {row}   [{time.perf_counter()-t:.1f}s]", flush=True)
    return row


with psycopg.connect(url, options="-c default_transaction_read_only=on") as conn:
    conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    print("transaction_read_only:", conn.execute("show transaction_read_only").fetchone()[0])

    # --- activities pre-aggregation ---
    q(conn, "activities: episode rows", "SELECT count(*) FROM atus.activities")
    q(conn, "activities: (tucaseid, activity_code) groups, min/max summed minutes, groups summing to 0",
      """SELECT count(*), min(m), max(m), count(*) FILTER (WHERE m = 0)
         FROM (SELECT tucaseid, activity_code, sum(duration_minutes) m
               FROM atus.activities GROUP BY 1,2) g""")
    q(conn, "activities: max per-respondent total minutes (all codes)",
      "SELECT max(m), min(m) FROM (SELECT tucaseid, sum(duration_minutes) m FROM atus.activities GROUP BY 1) g")
    q(conn, "activities: 0-minute episodes", "SELECT count(*) FROM atus.activities WHERE duration_minutes = 0")
    q(conn, "respondents without any activity row",
      "SELECT count(*) FROM atus.respondents r WHERE NOT EXISTS (SELECT 1 FROM atus.activities a WHERE a.tucaseid=r.tucaseid)")

    # --- replicate weights ---
    allnull = " AND ".join(f"{c} IS NULL" for c in R)
    anynull = " OR ".join(f"{c} IS NULL" for c in R)
    q(conn, "replicate_weights rows", "SELECT count(*) FROM atus.replicate_weights")
    q(conn, "replicate_weights all-NULL rows, of which data_year=2020, of which final_weight IS NULL",
      f"""SELECT count(*), count(*) FILTER (WHERE r.data_year=2020), count(*) FILTER (WHERE r.final_weight IS NULL)
          FROM atus.replicate_weights rw JOIN atus.respondents r USING (tucaseid) WHERE {allnull}""")
    q(conn, "replicate_weights rows with SOME but not all NULL",
      f"SELECT count(*) FROM atus.replicate_weights WHERE ({anynull}) AND NOT ({allnull})")
    q(conn, "respondents data_year=2020 / final_weight IS NULL / final_weight = 0",
      "SELECT count(*) FILTER (WHERE data_year=2020), count(*) FILTER (WHERE final_weight IS NULL), count(*) FILTER (WHERE final_weight = 0) FROM atus.respondents")
    q(conn, "respondents with final_weight > 0 and NO replicate_weights row",
      "SELECT count(*) FROM atus.respondents r WHERE r.final_weight > 0 AND NOT EXISTS (SELECT 1 FROM atus.replicate_weights w WHERE w.tucaseid=r.tucaseid)")
    q(conn, "replicate_weights rows with any zero / any negative cell",
      "SELECT count(*) FILTER (WHERE " + " OR ".join(f"{c} = 0" for c in R) + "), count(*) FILTER (WHERE " + " OR ".join(f"{c} < 0" for c in R) + ") FROM atus.replicate_weights")
    cells = " + ".join(f"({c} IS NOT NULL)::int" for c in R)
    differ = " + ".join(f"({c}::float8::numeric <> {c})::int" for c in R)
    q(conn, "replicate_weights non-NULL cells / cells where numeric->float8->numeric differs",
      f"SELECT sum({cells}), coalesce(sum({differ}),0) FROM atus.replicate_weights")
    q(conn, "replicate_weights max abs value / max significant digits proxy (max abs)",
      "SELECT max(greatest(" + ",".join(f"abs({c})" for c in R) + ")) FROM atus.replicate_weights")

    # --- pandemic replicate weights ---
    allzero = " AND ".join(f"{c} = 0" for c in P)
    panynull = " OR ".join(f"{c} IS NULL" for c in P)
    q(conn, "pandemic_replicate_weights rows", "SELECT count(*) FROM atus.pandemic_replicate_weights")
    q(conn, "pandemic all-zero rows, of which pandemic_weight=0, pandemic_weight IS NULL, pandemic_weight>0",
      f"""SELECT count(*), count(*) FILTER (WHERE r.pandemic_weight = 0),
                 count(*) FILTER (WHERE r.pandemic_weight IS NULL), count(*) FILTER (WHERE r.pandemic_weight > 0)
          FROM atus.pandemic_replicate_weights pw JOIN atus.respondents r USING (tucaseid) WHERE {allzero}""")
    q(conn, "pandemic rows with any NULL cell", f"SELECT count(*) FROM atus.pandemic_replicate_weights WHERE {panynull}")
    q(conn, "pandemic rows whose respondent has pandemic_weight = 0 but replicates NOT all zero",
      f"""SELECT count(*) FROM atus.pandemic_replicate_weights pw JOIN atus.respondents r USING (tucaseid)
          WHERE r.pandemic_weight = 0 AND NOT ({allzero})""")
    q(conn, "respondents pandemic_weight>0 / =0 / NULL",
      "SELECT count(*) FILTER (WHERE pandemic_weight>0), count(*) FILTER (WHERE pandemic_weight=0), count(*) FILTER (WHERE pandemic_weight IS NULL) FROM atus.respondents")
    q(conn, "respondents with pandemic_weight > 0 and NO pandemic replicate row",
      "SELECT count(*) FROM atus.respondents r WHERE r.pandemic_weight > 0 AND NOT EXISTS (SELECT 1 FROM atus.pandemic_replicate_weights w WHERE w.tucaseid=r.tucaseid)")
    pcells = " + ".join(f"({c} IS NOT NULL)::int" for c in P)
    pdiffer = " + ".join(f"({c}::float8::numeric <> {c})::int" for c in P)
    q(conn, "pandemic non-NULL cells / cells where numeric->float8->numeric differs",
      f"SELECT sum({pcells}), coalesce(sum({pdiffer}),0) FROM atus.pandemic_replicate_weights")

    # --- roster / CPS ---
    q(conn, "household_members rows / lineno=1 rows / respondents without a lineno=1 row",
      "SELECT (SELECT count(*) FROM atus.household_members), (SELECT count(*) FROM atus.household_members WHERE lineno=1), (SELECT count(*) FROM atus.respondents r WHERE NOT EXISTS (SELECT 1 FROM atus.household_members h WHERE h.tucaseid=r.tucaseid AND h.lineno=1))")
    q(conn, "household_members lineno=1: NULL age / NULL sex",
      "SELECT count(*) FILTER (WHERE age IS NULL), count(*) FILTER (WHERE sex IS NULL) FROM atus.household_members WHERE lineno=1")
    q(conn, "cps_persons rows / lineno=1 rows / respondents without a lineno=1 row",
      "SELECT (SELECT count(*) FROM atus.cps_persons), (SELECT count(*) FROM atus.cps_persons WHERE lineno=1), (SELECT count(*) FROM atus.respondents r WHERE NOT EXISTS (SELECT 1 FROM atus.cps_persons c WHERE c.tucaseid=r.tucaseid AND c.lineno=1))")
    q(conn, "cps_persons lineno=1: NULL region / state_fips / education",
      "SELECT count(*) FILTER (WHERE region IS NULL), count(*) FILTER (WHERE state_fips IS NULL), count(*) FILTER (WHERE education IS NULL) FROM atus.cps_persons WHERE lineno=1")
    q(conn, "respondents: NULL household_children / labor_force_status / diary_date",
      "SELECT count(*) FILTER (WHERE household_children IS NULL), count(*) FILTER (WHERE labor_force_status IS NULL), count(*) FILTER (WHERE diary_date IS NULL) FROM atus.respondents")
    conn.execute("COMMIT")
