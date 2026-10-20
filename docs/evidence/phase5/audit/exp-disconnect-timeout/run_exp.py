"""Disconnect experiment driver.

usage: run_exp.py <label> <n_requests> <activity_code> [<interval_s>]

1. starts poll_activity.py (1 Hz pg_stat_activity poll of the audit API's
   backends) in the background;
2. fires <n_requests> identical uncached POST /api/v1/analysis/trend requests
   (activity <activity_code>, 2003-2025, multiyear weights, replicate variance),
   each with `curl --max-time 1` (client gives up and closes the socket after
   1 s), sequentially;
3. waits for the poller to see the audit backends go idle;
4. sends the same request once more, waiting for the full response, to show
   whether the abandoned computation completed and was cached.
All output goes to <label>_*.txt next to this script.
"""

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = "$REPO/.venv/bin/python"
URL = "http://127.0.0.1:8202/api/v1/analysis/trend"

label, n, code = sys.argv[1], int(sys.argv[2]), sys.argv[3]
interval = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0
body = {
    "measure": "average_minutes_per_day",
    "activity": {"include": [code]},
    "years": list(range(2003, 2026)),
    "weights": "multiyear",
    "variance": "replicate",
}
body_path = HERE / f"{label}_body.json"
body_path.write_text(json.dumps(body))
log = open(HERE / f"{label}_driver.txt", "w", buffering=1)


def now() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


log.write(f"{now()} body={json.dumps(body)}\n")
poll_out = HERE / f"{label}_pgstat.txt"
poll_out.write_text("")
poller = subprocess.Popen(
    [PY, "-I", str(HERE / "poll_activity.py"), str(poll_out), "600", "3"]
)
time.sleep(1.5)

for i in range(n):
    t0 = time.monotonic()
    log.write(f"{now()} request {i + 1}: curl --max-time 1 start\n")
    proc = subprocess.run(
        [
            "curl", "-sS", "--max-time", "1", "-o", "/dev/null",
            "-w", "http_code=%{http_code} time_total=%{time_total}",
            "-H", "Content-Type: application/json",
            "--data-binary", f"@{body_path}", URL,
        ],
        capture_output=True, text=True,
    )
    log.write(
        f"{now()} request {i + 1}: curl exit={proc.returncode} "
        f"({'28 = timed out, client disconnected' if proc.returncode == 28 else 'see stderr'}) "
        f"stdout={proc.stdout!r} stderr={proc.stderr.strip()!r} "
        f"elapsed={time.monotonic() - t0:.2f}s\n"
    )
    if interval:
        time.sleep(interval)

t_all = time.monotonic()
log.write(f"{now()} all {n} client(s) disconnected; waiting for poller to see backends idle\n")
poller.wait()
log.write(f"{now()} poller finished ({time.monotonic() - t_all:.1f}s after last disconnect)\n")

time.sleep(1.0)
t0 = time.monotonic()
proc = subprocess.run(
    [
        "curl", "-sS", "-D", "-", "-o", str(HERE / f"{label}_followup_response.json"),
        "-w", "http_code=%{http_code} time_total=%{time_total}\n",
        "-H", "Content-Type: application/json",
        "--data-binary", f"@{body_path}", URL,
    ],
    capture_output=True, text=True,
)
log.write(
    f"{now()} follow-up identical request (full wait): exit={proc.returncode} "
    f"elapsed={time.monotonic() - t0:.3f}s\n{proc.stdout}\n{proc.stderr}\n"
)
log.close()
