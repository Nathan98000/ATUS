"""Abandoned request + API process signal.
usage: run_kill.py <label> <server_pid> <signal: TERM|KILL> <activity_code>
Fires one `curl --max-time 1` trend request (2003-2025, replicate), sends the
signal to the API process 2 s after the client disconnected, then polls
pg_stat_activity (1 Hz) until the audit backends are gone/idle, and records
when the API process actually exited."""
import json, os, signal, subprocess, sys, time
from datetime import datetime
from pathlib import Path
HERE = Path(__file__).resolve().parent
PY = "$REPO/.venv/bin/python"
label, pid, sig, code = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
body = HERE / f"{label}_body.json"
body.write_text(json.dumps({"measure": "average_minutes_per_day", "activity": {"include": [code]},
                            "years": list(range(2003, 2026)), "weights": "multiyear",
                            "variance": "replicate"}))
log = open(HERE / f"{label}_driver.txt", "w", buffering=1)
now = lambda: datetime.now().strftime("%H:%M:%S.%f")[:-3]
poll_out = HERE / f"{label}_pgstat.txt"; poll_out.write_text("")
poller = subprocess.Popen([PY, "-I", str(HERE / "poll_activity.py"), str(poll_out), "300", "3"])
time.sleep(1.2)
log.write(f"{now()} curl --max-time 1 start\n")
p = subprocess.run(["curl", "-sS", "--max-time", "1", "-o", "/dev/null", "-w", "%{http_code}",
                    "-H", "Content-Type: application/json", "--data-binary", f"@{body}",
                    "http://127.0.0.1:8202/api/v1/analysis/trend"], capture_output=True, text=True)
log.write(f"{now()} curl exit={p.returncode} {p.stderr.strip()}\n")
time.sleep(2.0)
log.write(f"{now()} sending SIG{sig} to API pid {pid}\n")
os.kill(pid, getattr(signal, f"SIG{sig}"))
t0 = time.monotonic()
exited_at = None
while time.monotonic() - t0 < 300:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        exited_at = now(); break
    time.sleep(0.1)
log.write(f"{now()} API process exited at {exited_at} ({time.monotonic() - t0:.1f}s after signal)\n")
poller.wait()
log.write(f"{now()} poller done\n")
log.close()
print(open(HERE / f"{label}_driver.txt").read())
