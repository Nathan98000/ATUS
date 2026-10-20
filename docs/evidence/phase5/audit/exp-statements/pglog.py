"""Parse the container's Postgres log (docker logs) into entries."""
import re, subprocess
from datetime import datetime

PREFIX = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) \w+ \[(\d+)\] (\S*) (\S*)@(\S*) (\w+):  (.*)$")

def read_entries(container="atus-audit-stmt", since=None):
    cmd = ["docker", "logs", container]
    if since:
        cmd += ["--since", since]
    out = subprocess.run(cmd, capture_output=True, text=True)
    text = out.stdout + out.stderr  # postgres logs to stderr
    lines = (out.stderr).splitlines() if out.stderr else out.stdout.splitlines()
    entries = []
    for raw in lines:
        m = PREFIX.match(raw)
        if m:
            ts, pid, app, user, db, level, msg = m.groups()
            entries.append({"ts": ts, "pid": int(pid), "app": app, "user": user, "db": db,
                            "level": level, "msg": msg, "raw": raw})
        elif entries:
            entries[-1]["msg"] += "\n" + raw
            entries[-1]["raw"] += "\n" + raw
    entries.sort(key=lambda e: e["ts"])  # stable
    return entries

def is_statement(e):
    return e["level"] == "LOG" and (e["msg"].startswith("statement: ") or e["msg"].startswith("execute "))

def short(e):
    msg = e["msg"]
    if msg.startswith("statement: "):
        body = msg[len("statement: "):]
        kind = "simple"
    else:
        kind, _, body = msg.partition(": ")
        kind = kind  # "execute <unnamed>" / "execute _pg3_0"
    body = " ".join(body.split())
    return kind, body
