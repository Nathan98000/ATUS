"""Deep in-memory size (sys.getsizeof, recursive, de-duplicated by id) of the
payload dict the API caches per analysis (engine result .to_dict(), exactly
what routes/analysis.py:_cached_analysis stores), plus key + LRU node overhead.
Runs the real engine against the prototype serving DB (read-only session)."""
import json
import sys
from collections import OrderedDict

import psycopg

sys.path.insert(0, "$REPO/src")
from atus_pipeline.api.schemas.analysis import CompareRequest, EstimateRequest, TrendRequest  # noqa: E402
from atus_pipeline.analytics import AnalysisEngine  # noqa: E402


def deep(o, seen=None):
    seen = seen if seen is not None else set()
    if id(o) in seen:
        return 0
    seen.add(id(o))
    s = sys.getsizeof(o)
    if isinstance(o, dict):
        s += sum(deep(k, seen) + deep(v, seen) for k, v in o.items())
    elif isinstance(o, (list, tuple, set, frozenset)):
        s += sum(deep(x, seen) for x in o)
    return s


YEARS_ALL = list(range(2003, 2026))
cases = {
    "estimate_1y_SE": (EstimateRequest, {"activity": {"include": ["0101"]}, "years": [2024]}, "estimate"),
    "estimate_pooled22_SE": (EstimateRequest, {"activity": {"preset": "leisure_and_sports_bls_table"},
                                               "years": [y for y in YEARS_ALL if y != 2020]}, "estimate"),
    "compare_1y_SE": (CompareRequest, {"activity": {"include": ["0101"]}, "years": [2024],
                                       "group_a": {"sex": "male"}, "group_b": {"sex": "female"}}, "compare"),
    "trend23_SE": (TrendRequest, {"activity": {"include": ["0101"]}, "years": YEARS_ALL}, "trend"),
}
out = {}
with psycopg.connect("postgresql://atus:atus@localhost:5441/atus",
                     options="-c jit=off -c default_transaction_read_only=on") as conn:
    for name, (model, body, op) in cases.items():
        eng = AnalysisEngine(conn)
        req = model(**body)
        if op == "compare":
            payload = eng.compare(req.to_comparison()).to_dict()
        elif op == "trend":
            payload = eng.trend(req.to_spec()).to_dict()
        else:
            payload = eng.estimate(req.to_spec()).to_dict()
        conn.rollback()
        key = "f" * 64
        od = OrderedDict()
        base = sys.getsizeof(od)
        od[key] = payload
        node_overhead = sys.getsizeof(od) - base + 100  # dict slot growth + ~100 B OrderedDict link node (approx)
        out[name] = {"payload_deep_bytes": deep(payload), "key_bytes": sys.getsizeof(key),
                     "json_bytes": len(json.dumps(payload)), "approx_lru_overhead_bytes": node_overhead}
        print(name, out[name])
json.dump(out, open(__file__.replace(".py", ".json"), "w"), indent=1)
