"""Read-only verification of replicate-weight row-drop assumptions on the full DB."""
import psycopg, sys
URL = "postgresql://atus:atus@localhost:5434/atus"
rw = ",".join(f"tufnwgtp{i:03d}" for i in range(1, 161))
pw = ",".join(f"tu20fwgt{i:03d}" for i in range(1, 161))
pw_zero = " AND ".join(f"tu20fwgt{i:03d} = 0" for i in range(1, 161))
pw_any_pos = " OR ".join(f"tu20fwgt{i:03d} <> 0" for i in range(1, 161))
queries = {
 "rw_null_pattern_by_year_and_final_weight": f"""
   SELECT r.data_year, (r.final_weight IS NULL) AS fw_null, (r.final_weight = 0) AS fw_zero,
          num_nulls(VARIADIC ARRAY[{rw}]) AS n_null_cols, count(*)
   FROM atus.replicate_weights w JOIN atus.respondents r USING (tucaseid)
   WHERE num_nulls(VARIADIC ARRAY[{rw}]) > 0 OR r.final_weight IS NULL OR r.final_weight = 0
   GROUP BY 1,2,3,4 ORDER BY 1,2,3,4""",
 "rw_all_null_rows": f"SELECT count(*) FROM atus.replicate_weights WHERE num_nulls(VARIADIC ARRAY[{rw}]) = 160",
 "rw_zero_cells_in_rows_with_final_weight_gt0": f"""
   SELECT count(*) FROM atus.replicate_weights w JOIN atus.respondents r USING (tucaseid)
   WHERE r.final_weight > 0 AND num_nulls(VARIADIC ARRAY[{rw}]) > 0""",
 "respondents_fw_gt0_without_rw_row": """
   SELECT count(*) FROM atus.respondents r WHERE r.final_weight > 0
   AND NOT EXISTS (SELECT 1 FROM atus.replicate_weights w WHERE w.tucaseid = r.tucaseid)""",
 "respondents_pw_gt0_without_pw_row": """
   SELECT count(*) FROM atus.respondents r WHERE r.pandemic_weight > 0
   AND NOT EXISTS (SELECT 1 FROM atus.pandemic_replicate_weights w WHERE w.tucaseid = r.tucaseid)""",
 "pw_all_zero_rows_by_year_and_pandemic_weight": f"""
   SELECT r.data_year, r.pandemic_weight IS NULL AS pw_null, r.pandemic_weight = 0 AS pw_zero, count(*)
   FROM atus.pandemic_replicate_weights w JOIN atus.respondents r USING (tucaseid)
   WHERE {pw_zero} GROUP BY 1,2,3 ORDER BY 1,2,3""",
 "pw_rows_with_nulls": f"SELECT count(*) FROM atus.pandemic_replicate_weights WHERE num_nulls(VARIADIC ARRAY[{pw}]) > 0",
 "pw_zero_weight_respondents_with_nonzero_reps": f"""
   SELECT count(*) FROM atus.pandemic_replicate_weights w JOIN atus.respondents r USING (tucaseid)
   WHERE (r.pandemic_weight = 0 OR r.pandemic_weight IS NULL) AND ({pw_any_pos})""",
 "pw_pos_weight_with_all_zero_reps": f"""
   SELECT count(*) FROM atus.pandemic_replicate_weights w JOIN atus.respondents r USING (tucaseid)
   WHERE r.pandemic_weight > 0 AND {pw_zero}""",
 "respondent_weight_profile": """
   SELECT data_year IN (2019,2020) AS y1920,
          count(*) FILTER (WHERE final_weight IS NULL) fw_null,
          count(*) FILTER (WHERE final_weight = 0) fw_zero,
          count(*) FILTER (WHERE final_weight > 0) fw_pos,
          count(*) FILTER (WHERE pandemic_weight IS NULL) pw_null,
          count(*) FILTER (WHERE pandemic_weight = 0) pw_zero,
          count(*) FILTER (WHERE pandemic_weight > 0) pw_pos,
          count(*) total
   FROM atus.respondents GROUP BY 1 ORDER BY 1""",
 "pw_table_rows_by_year": """SELECT r.data_year, count(*) FROM atus.pandemic_replicate_weights w
   JOIN atus.respondents r USING (tucaseid) GROUP BY 1 ORDER BY 1""",
 "sum_types": """SELECT pg_typeof(sum(1::smallint)) s_smallint, pg_typeof(sum(1::int)) s_int,
   pg_typeof(sum(1::bigint)) s_bigint, pg_typeof(sum(1::numeric)) s_numeric""",
}
with psycopg.connect(URL) as conn:
    conn.execute("SET default_transaction_read_only = on")
    conn.commit()
    for name, sql in queries.items():
        rows = conn.execute(sql).fetchall()
        print(f"== {name}")
        for r in rows:
            print("  ", r)
    conn.rollback()
