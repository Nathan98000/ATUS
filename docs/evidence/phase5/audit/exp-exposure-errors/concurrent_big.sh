#!/bin/bash
S=docs/evidence/phase5/audit/exp-exposure-errors
N=$1; FILE=$2
for i in $(seq 1 $N); do
  curl -s -o /dev/null -w "req$i status=%{http_code} t=%{time_total}s up=%{size_upload}\n" -X POST -H 'Content-Type: application/json' --data-binary @$S/payloads/$FILE http://127.0.0.1:8206/api/v1/analysis/estimate &
done
wait
