#!/bin/bash
# usage: run_exp.sh <name> <data_ver v1|v2> <hold_s> <traffic_s> <workers> <loader_delay_s> [inflight_hold_s] [mix]
set -u
S=docs/evidence/phase5/audit/exp-reload
PY=$REPO/.venv/bin/python
NAME=$1; VER=$2; HOLD=$3; TDUR=$4; WORKERS=$5; LDELAY=$6; INFL=${7:-0}; MIX=${8:-ready,meta,activities,est_cached,est_uncached,est_uncached}
DB=atus_audit_reload
OUT=$S/$NAME; mkdir -p $OUT
cd $S
# warm the cached spec under the current data_version
curl -s -o /dev/null -w "warm est_cached: %{http_code}\n" -X POST http://127.0.0.1:8201/api/v1/analysis/estimate -H 'content-type: application/json' -d '{"activity":{"include":["010101"]},"years":[2023]}'
curl -s -o /dev/null -w "warm est_cached: %{http_code}\n" -X POST http://127.0.0.1:8201/api/v1/analysis/estimate -H 'content-type: application/json' -d '{"activity":{"include":["010101"]},"years":[2023]}'
$PY -c 'import json,time;print(json.dumps({"t":time.time(),"event":"exp_start"}))' > $OUT/events.jsonl
$PY sampler.py $DB $OUT/locks.jsonl $((TDUR + 10)) &
SP=$!
$PY traffic.py $OUT/requests.csv $TDUR $WORKERS "$MIX" > $OUT/traffic.out 2>&1 &
TP=$!
if [ "$INFL" != "0" ]; then
  sleep $((LDELAY - 2))
  $PY inflight.py $DB $INFL $OUT/events.jsonl &
  IP=$!
  sleep 2
else
  sleep $LDELAY
fi
$PY run_loader.py $DB $S/tmp_$VER/data $HOLD $OUT/events.jsonl > $OUT/loader.out 2>&1
echo "loader exit: $?" | tee -a $OUT/loader.out
wait $TP; wait $SP
[ "$INFL" != "0" ] && wait $IP
cat $OUT/traffic.out
