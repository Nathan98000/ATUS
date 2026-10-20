#!/bin/bash
# usage: rss_during.sh <pid> <label> <command...> ; samples RSS (KB) every 50ms while command runs
S=docs/evidence/phase5/audit/exp-exposure-errors
PID=$1; LABEL=$2; shift 2
OUT=$S/raw/rss_$LABEL.txt
before=$(ps -o rss= -p $PID | tr -d ' ')
( while true; do ps -o rss= -p $PID | tr -d ' '; /bin/sleep 0.05; done ) > $OUT.samples &
SP=$!
"$@"
kill $SP 2>/dev/null; wait $SP 2>/dev/null
after=$(ps -o rss= -p $PID | tr -d ' ')
peak=$(sort -n $OUT.samples | tail -1)
echo "label=$LABEL rss_before_kb=$before rss_peak_kb=$peak rss_after_kb=$after samples=$(wc -l < $OUT.samples)" | tee $OUT
