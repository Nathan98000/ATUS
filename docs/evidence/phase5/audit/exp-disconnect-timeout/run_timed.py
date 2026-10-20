"""Send one request and wait for the full response, polling pg_stat_activity
(+ /dev/shm) at 1 Hz meanwhile. usage: run_timed.py <label> <endpoint> <body.json>"""
import subprocess, sys, time
from datetime import datetime
from pathlib import Path
HERE = Path(__file__).resolve().parent
PY = "$REPO/.venv/bin/python"
label, endpoint, body = sys.argv[1], sys.argv[2], sys.argv[3]
poll_out = HERE / f"{label}_pgstat.txt"; poll_out.write_text("")
poller = subprocess.Popen([PY, "-I", str(HERE / "poll_activity.py"), str(poll_out), "900", "2"])
time.sleep(1.2)
t0 = time.monotonic()
start = datetime.now().strftime("%H:%M:%S.%f")[:-3]
p = subprocess.run(["curl", "-sS", "-D", "-", "-o", str(HERE / f"{label}_response.json"),
                    "-w", "http_code=%{http_code} time_total=%{time_total}\n",
                    "-H", "Content-Type: application/json", "--data-binary", f"@{body}",
                    f"http://127.0.0.1:8202{endpoint}"], capture_output=True, text=True)
el = time.monotonic() - t0
poller.wait()
with open(HERE / f"{label}_driver.txt", "w") as f:
    f.write(f"start={start} endpoint={endpoint} body={Path(body).read_text()}\n"
            f"elapsed={el:.3f}s exit={p.returncode}\n{p.stdout}\n{p.stderr}\n")
print(open(HERE / f"{label}_driver.txt").read())
