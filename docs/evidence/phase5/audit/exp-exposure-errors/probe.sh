#!/bin/bash
# usage: probe.sh <port> <outname> <curl args...>
PORT=$1; NAME=$2; shift 2
S=docs/evidence/phase5/audit/exp-exposure-errors
OUT=$S/raw/$NAME.txt
{
  echo "### curl $* (port $PORT)"
  curl -sS -i --max-time 120 -w '\n### status=%{http_code} time_total=%{time_total}s size_download=%{size_download} size_upload=%{size_upload}\n' "$@" 2>&1
} > "$OUT"
# print a compact summary: status line + error code + size
STATUS=$(grep -m1 '^HTTP/' "$OUT" | tr -d '\r')
TAIL=$(grep '^### status=' "$OUT")
echo "$NAME | $STATUS | $TAIL"
