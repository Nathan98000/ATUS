#!/bin/bash
# Fresh API instance on 8216 (fixture DB); measure RSS growth from large JSON bodies.
S=docs/evidence/phase5/audit/exp-exposure-errors
start() {
  PYTHONDONTWRITEBYTECODE=1 ATUS_DATABASE_URL=postgresql://atus:atus@localhost:5434/atus_audit_probe ATUS_LOG_LEVEL=WARNING nohup $REPO/.venv/bin/atus api --port 8216 > $S/server_8216_memtest.log 2>&1 &
  echo $! > $S/server_8216.pid
  for i in $(seq 1 40); do curl -s -o /dev/null http://127.0.0.1:8216/health && break; /bin/sleep 0.25; done
  curl -s -o /dev/null -X POST -H 'Content-Type: application/json' --data-binary @$S/payloads/valid.json http://127.0.0.1:8216/api/v1/analysis/estimate
}
stop() { kill $(cat $S/server_8216.pid); /bin/sleep 1; }
run() { # label N file
  local label=$1 n=$2 file=$3
  start; PID=$(cat $S/server_8216.pid)
  before=$(ps -o rss= -p $PID | tr -d ' ')
  ( while true; do ps -o rss= -p $PID | tr -d ' '; /bin/sleep 0.02; done ) > $S/raw/memtest_$label.samples &
  SP=$!
  CP=""
  for i in $(seq 1 $n); do curl -s -o /dev/null -w "  req$i status=%{http_code} t=%{time_total}s up=%{size_upload}\n" -X POST -H 'Content-Type: application/json' --data-binary @$S/payloads/$file http://127.0.0.1:8216/api/v1/analysis/estimate & CP="$CP $!"; done
  wait $CP
  /bin/sleep 0.3; kill $SP; wait $SP 2>/dev/null
  after=$(ps -o rss= -p $PID | tr -d ' ')
  peak=$(sort -n $S/raw/memtest_$label.samples | tail -1)
  echo "$label: concurrent=$n body=$file rss_before_kb=$before rss_peak_kb=$peak rss_after_kb=$after"
  stop
}
run baseline_valid 1 valid.json
run one_5mb 1 big5mb.json
run one_50mb 1 big50mb.json
run two_50mb 2 big50mb.json
run three_50mb 3 big50mb.json
run one_100mb 1 big100mb.json
run eight_5mb 8 big5mb.json
