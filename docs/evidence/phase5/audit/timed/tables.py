"""Render markdown tables from the raw JSON outputs (used to build REPORT.md)."""
import json
import sys
from pathlib import Path

D = Path(__file__).resolve().parent


def f(x, nd=2):
    return "-" if x is None else f"{x:.{nd}f}"


def matrix_table(label):
    p = D / f"matrix_{label}.json"
    if not p.exists():
        return f"(missing {p.name})\n"
    m = json.loads(p.read_text())
    lines = [f"Source: `{p.name}` / `matrix_{label}.csv` (raw rows), pg log `matrix_{label}_pglog.txt.gz`; "
             f"DB {m['db_container']} {m['db_limits']}; API {m['api_limits']}; session jit={m['session_jit']}; "
             f"run {m['date_utc']} -> {m['date_end_utc']}", "",
             "| request | n | status | X-Cache | total wall p50 / p95 / max (s) | DB time p50 / p95 (s) | of which analysis SQL p50 (s) | API = total - DB p50 / p95 (s) | DB stmts/request | warm-up run (s) |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for t, s in m["summary"].items():
        w, d, a, api = s["wall_s"], s["db_ms"], s["analysis_sql_ms"], s["api_ms_total_minus_db"]
        if w.get("n", 0) == 0:
            continue
        ms = lambda dd, k: None if not dd or dd.get("n", 0) == 0 else dd[k] / 1000
        warm = ", ".join(f(x["wall_s"]) for x in s["warmup_round0"])
        lines.append(
            f"| {t} | {s['n']} | {','.join(s['statuses'])} | {','.join(s['x_cache'])} | "
            f"{f(w['p50'])} / {f(w['p95'])} / {f(w['max'])} | {f(ms(d,'p50'))} / {f(ms(d,'p95'))} | "
            f"{f(ms(a,'p50'))} | {f(ms(api,'p50'),3)} / {f(ms(api,'p95'),3)} | "
            f"{','.join(map(str, s['db_statements']))} | {warm} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    for lab in sys.argv[1:]:
        print(matrix_table(lab))
