#!/bin/bash
# Exp E: clients that give up (2 s client timeout, like an impatient browser/proxy) during a held load:
# do their server-side requests release pool connections?
S=docs/evidence/phase5/audit/exp-reload
PY=$REPO/.venv/bin/python
OUT=$S/expE; mkdir -p $OUT; cd $S
$PY run_loader.py atus_audit_reload $S/tmp_v2/data 25 $OUT/events.jsonl > $OUT/loader.out 2>&1 &
LP=$!
sleep 2
for i in $(seq 1 12); do
  curl -s -m 2 -o /dev/null -w "client$i: http=%{http_code} t=%{time_total}\n" -X POST http://127.0.0.1:8201/api/v1/analysis/estimate \
    -H 'content-type: application/json' -d "{\"activity\":{\"include\":[\"010101\"]},\"years\":[2023],\"confidence_level\":0.8$i}" &
done
wait $(jobs -p | grep -v $LP) 2>/dev/null
sleep 3
echo "--- 5 s after all clients disconnected:"
curl -s http://127.0.0.1:8201/__audit/threads; echo
$PY - <<'EOF'
import psycopg
with psycopg.connect("postgresql://atus:atus@localhost:5434/atus_audit_reload?application_name=expE_ctl", autocommit=True) as c:
    rows = c.execute("SELECT state, wait_event_type, wait_event, count(*) FROM pg_stat_activity WHERE datname=current_database() "
                     "AND application_name='' GROUP BY 1,2,3").fetchall()
    print("API backends (state, wait_type, wait_event, n):", rows)
EOF
echo "--- a new cached-spec request now (pool should be exhausted):"
curl -s -o /dev/null -w "http=%{http_code} t=%{time_total}\n" -X POST http://127.0.0.1:8201/api/v1/analysis/estimate -H 'content-type: application/json' -d '{"activity":{"include":["010101"]},"years":[2023]}'
wait $LP; echo "loader exit $?"
sleep 1
curl -s http://127.0.0.1:8201/__audit/threads; echo
grep -c "data version changed" $S/api_server2.log
