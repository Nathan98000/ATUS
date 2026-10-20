S=$1; N=$2
ATUS_DATABASE_URL=postgresql://atus:atus@localhost:5434/atus_audit_nonexistent ATUS_LOG_LEVEL=WARNING $REPO/.venv/bin/atus api --port 8291 > $S/server_$N.log 2>&1 &
for i in $(seq 1 50); do curl -s -o /dev/null http://127.0.0.1:8291/docs && break; sleep 0.2; done
SPID=$(lsof -tiTCP:8291 -sTCP:LISTEN)
B=$(ps -o rss= -p $SPID)
( while kill -0 $SPID 2>/dev/null; do ps -o rss= -p $SPID; sleep 0.02; done ) > $S/samples_$N.txt &
SAM=$!
P=""
for n in $(seq 1 $N); do curl -s -m 60 -o $S/resp_${N}_$n.txt -w "code=%{http_code} t=%{time_total} up=%{size_upload}\n" -H 'Content-Type: application/json' --data-binary @$S/big50.json http://127.0.0.1:8291/api/v1/analysis/estimate & P="$P $!"; done
wait $P; sleep 1
echo "concurrent=$N before_kb=$B peak_kb=$(sort -n $S/samples_$N.txt | tail -1) after_kb=$(ps -o rss= -p $SPID)"
head -c 300 $S/resp_${N}_1.txt; echo
kill $SAM; kill $SPID; sleep 1
