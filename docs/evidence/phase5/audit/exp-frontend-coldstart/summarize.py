#!/usr/bin/env python3
"""Summarize runs/<scenario>/<page>.json into a readable report (summary.txt)."""
import json
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs")


def short(text, n=230):
    return re.sub(r"\s+", " ", text or "").strip()[:n]


def key_text(snap, page):
    main = snap.get("main", "")
    if snap.get("alerts"):
        return "ALERT: " + short(" | ".join(snap["alerts"]), 330)
    if page == "home":
        # the starter-examples section is what depends on /meta
        m = re.search(r"Start from a question(.*?)What you are looking at", main, re.S)
        return "starter: " + short(m.group(1) if m else main, 200)
    if snap.get("status"):
        return "STATUS: " + short(" | ".join(s for s in snap["status"] if s), 260)
    return short(main, 200)


out = []
for scen in sorted(os.listdir(ROOT)):
    d = os.path.join(ROOT, scen)
    if not os.path.isdir(d):
        continue
    out.append(f"\n################ {scen}")
    for page in ("home", "explore", "deep"):
        f = os.path.join(d, f"{page}.json")
        if not os.path.exists(f):
            continue
        r = json.load(open(f))
        reqs = [e for e in r["network"] if e["ev"] == "request"]
        by_path = {}
        for e in reqs:
            by_path.setdefault(f'{e["method"]} {e["url"].split("?")[0]}', []).append(e["t"])
        out.append(f"--- {page}  ({r['url'][:60]}...)")
        for k, v in by_path.items():
            out.append(f"    req {k}: n={len(v)} at {v}")
        resp = [f'{e["url"].split("/")[-1]}:{e["status"]}@{e["t"]}' for e in r["network"] if e["ev"] == "response"]
        fails = [f'{e["url"].split("/")[-1]}:{e.get("failure")}@{e["t"]}' for e in r["network"] if e["ev"] == "requestfailed"]
        out.append(f"    responses: {resp[:14]}")
        if fails:
            out.append(f"    failed: {fails[:10]}")
        for s in r["snapshots"]:
            out.append(f"    @{s['checkpoint_s']:>3}s title={s.get('title','')[:50]!r}: {key_text(s, page)}")
        last = r["snapshots"][-1]
        foot = re.search(r"Data release.*", last.get("footer", ""))
        out.append(f"    footer(last): {foot.group(0) if foot else '(no version line)'}")
        out.append("    transitions: " + " || ".join(f"{t['t']:.1f}s:{short(t['main'], 90)}" for t in r["timeline"]))
        cons = [c for c in r["console"] if c["type"] in ("error", "pageerror", "warning")]
        for c in cons[:5]:
            out.append(f"    console {c['type']}@{c['t']}: {short(c['text'], 200)}")
        if len(cons) > 5:
            out.append(f"    ... {len(cons) - 5} more console errors")
text = "\n".join(out)
open(os.path.join(os.path.dirname(ROOT), "summary.txt"), "w").write(text)
print(text if len(sys.argv) < 2 else "\n".join(l for l in out if sys.argv[1] in l or True))
