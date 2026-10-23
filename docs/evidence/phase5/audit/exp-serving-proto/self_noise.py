"""Run-to-run SE noise on ONE database (plan/parallel summation order).
usage: python self_noise.py <url> <label> <repeats>"""
import sys, psycopg
from atus_pipeline.analytics import ActivitySelector, AnalysisEngine, AnalysisSpec, Measure
url, label, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
sleep = ActivitySelector.preset("sleep")
specs = {
  "E1 sleep 2025": ("estimate", AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, (2025,))),
  "T1 sleep trend 2003-2025": ("trend", AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, tuple(range(2003, 2026)))),
  "E3 sleep pooled 22y": ("estimate", AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, tuple(y for y in range(2003, 2026) if y != 2020))),
}
def ses(res):
    d = res.to_dict()
    if "points" in d:
        return [p["estimate"]["standard_error"] for p in d["points"] if p["estimate"]], [p["estimate"]["value"] for p in d["points"] if p["estimate"]]
    return [d["estimate"]["standard_error"]], [d["estimate"]["value"]]
with psycopg.connect(url, options="-c default_transaction_read_only=on") as conn:
    print(label, "max_parallel_workers_per_gather =", conn.execute("show max_parallel_workers_per_gather").fetchone()[0])
    eng = AnalysisEngine(conn)
    for name, (mode, spec) in specs.items():
        runs = [ses(getattr(eng, mode)(spec)) for _ in range(n)]
        base_se, base_v = runs[0]
        maxrel = max(abs(a - b) / a for r in runs[1:] for a, b in zip(base_se, r[0]))
        vsame = all(r[1] == base_v for r in runs[1:])
        print(f"{label}: {name}: {n} runs, max rel SE diff vs run 1 = {maxrel:.2e}; point values identical across runs: {vsame}", flush=True)
