import json, re, sys
from pathlib import Path
d = Path(__file__).with_name("logs")
raw = (d / sys.argv[1]).read_text() if len(sys.argv) > 1 else (d / "stmt_count_pg.log").read_text()
meta = json.loads((d / (sys.argv[2] if len(sys.argv) > 2 else "stmt_count.json")).read_text())
api = set(meta["api_pids"])
logre = re.compile(r"^\S+ \S+ \S+ \[(\d+)\] (LOG|DETAIL|ERROR|FATAL|STATEMENT|WARNING):  (.*)$")
cur, per, order = None, {}, []
lines = raw.splitlines()
i = 0
while i < len(lines):
    m = logre.match(lines[i])
    if not m:
        i += 1; continue
    pid, lvl, msg = int(m.group(1)), m.group(2), m.group(3)
    if lvl == "DETAIL" and "AUDITMARK " in msg:
        cur = msg.split("AUDITMARK ", 1)[1].rstrip("'")
        per[cur] = []; order.append(cur)
    elif pid in api and cur is not None and lvl == "LOG":
        mm = re.match(r"(statement|execute [^:]*): ?(.*)", msg)
        if mm:
            text = mm.group(2)
            j = i + 1
            while not text.strip() and j < len(lines) and lines[j].startswith("\t"):
                text = lines[j].strip(); j += 1
            per[cur].append(("prepared " if "_pg3_" in mm.group(1) else "") + text[:70])
    elif pid in api and cur is not None and lvl in ("ERROR", "FATAL"):
        per[cur].append(f"[{lvl}] {msg[:70]}")
    i += 1
res = {r[0]: r for r in meta["results"]}
for k in order:
    st = per[k]
    n = sum(1 for s in st if not s.startswith("["))
    r = res.get(k)
    print(f"{(r[1] if r else ''):>4} {str(r[2] if r else ''):>5} n={n:2d} | {k}")
    for s in st:
        print("        ", s)
