#!/bin/bash
# Start the real API (`atus api`) on 8204 against the scratch DB on 5442.
S=docs/evidence/phase5/audit/exp-statements
LOG=${1:-$S/api.log}
cd $S  # no .env here
export ATUS_DATABASE_URL='postgresql://atus:atus@127.0.0.1:5442/atus?application_name=atus-api'
export ATUS_LOG_LEVEL=INFO
nohup $REPO/.venv/bin/atus api --host 127.0.0.1 --port 8204 > "$LOG" 2>&1 &
echo $! > $S/api.pid
