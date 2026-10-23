"""Closed-loop concurrent HTTP traffic against the API on :8201, one CSV row per request.

usage: traffic.py <out.csv> <duration_s> <workers> [mix]
  mix: comma list of kinds cycled by each worker (offset per worker), default
       ready,meta,activities,est_cached,est_uncached,est_uncached
A dedicated prober also hits GET /health every 0.25 s (kind=health_probe) and
POST est_cached every 1 s (kind=est_cached_probe), independent of the workers.
"""
from __future__ import annotations

import asyncio
import csv
import itertools
import json
import sys
import time

import os

import httpx

THINK = float(os.environ.get("THINK", "0.1"))  # per-worker think time between requests

BASE = "http://127.0.0.1:8201"
out_path, duration, workers = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])
mix = (sys.argv[4] if len(sys.argv) > 4 else
       "ready,meta,activities,est_cached,est_uncached,est_uncached").split(",")

CACHED_BODY = {"activity": {"include": ["010101"]}, "years": [2023]}
_cl = itertools.count(1)


def uncached_body():
    # unique confidence level -> unique canonical spec -> guaranteed cache miss
    n = next(_cl)
    return {"activity": {"include": ["010101"]}, "years": [2023],
            "confidence_level": round(0.9 + n * 1e-6, 6)}


FIELDS = ["kind", "worker", "t_start", "t_end", "dur_s", "status", "error_code",
          "x_cache", "value", "run_id", "analysis_key", "exc"]
rows: list[dict] = []


async def one(client: httpx.AsyncClient, kind: str, worker: str):
    t0 = time.time()
    rec = {"kind": kind, "worker": worker, "t_start": f"{t0:.3f}"}
    try:
        if kind in ("health", "health_probe"):
            r = await client.get("/health")
        elif kind in ("popmeta", "popmeta_probe"):
            r = await client.get("/api/v1/population/metadata")
        elif kind == "presets":
            r = await client.get("/api/v1/activities/presets")
        elif kind == "threads_probe":
            r = await client.get("/__audit/threads")
        elif kind == "ready":
            r = await client.get("/ready")
        elif kind == "meta":
            r = await client.get("/api/v1/meta")
        elif kind == "activities":
            r = await client.get("/api/v1/activities")
        elif kind in ("est_cached", "est_cached_probe"):
            r = await client.post("/api/v1/analysis/estimate", json=CACHED_BODY)
        elif kind == "est_uncached":
            r = await client.post("/api/v1/analysis/estimate", json=uncached_body())
        else:
            raise ValueError(kind)
        t1 = time.time()
        rec.update(status=r.status_code, x_cache=r.headers.get("x-cache", ""),
                   analysis_key=r.headers.get("x-analysis-key", "")[:12])
        try:
            body = r.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            if "error" in body and isinstance(body["error"], dict):
                rec["error_code"] = body["error"].get("code")
            if "estimate" in body:
                rec["value"] = body["estimate"].get("value")
            if kind == "meta" and "data" in body:
                rec["run_id"] = body["data"].get("ingestion_run_id")
                rec["value"] = body["data"].get("respondents")
            if kind == "threads_probe":
                rec["value"] = "b{borrowed}/w{tasks_waiting}/pa{pool_available}/pw{requests_waiting}".format(**body)
            if kind == "activities" and "total" in body:
                rec["value"] = body["total"]
    except Exception as exc:
        t1 = time.time()
        rec.update(status=0, exc=f"{type(exc).__name__}: {exc}"[:200])
    rec.update(t_end=f"{t1:.3f}", dur_s=f"{t1 - t0:.3f}")
    rows.append(rec)


async def worker(client, idx, stop_at):
    cyc = itertools.cycle(mix[idx % len(mix):] + mix[: idx % len(mix)])
    while time.time() < stop_at:
        await one(client, next(cyc), f"w{idx}")
        await asyncio.sleep(THINK)


async def prober(client, kind, every, stop_at):
    while time.time() < stop_at:
        asyncio.create_task(one(client, kind, "probe"))  # fire-and-forget: never blocked by a slow previous probe
        await asyncio.sleep(every)


async def main():
    stop_at = time.time() + duration
    limits = httpx.Limits(max_connections=workers + 200, max_keepalive_connections=workers + 200)
    async with httpx.AsyncClient(base_url=BASE, timeout=httpx.Timeout(300.0), limits=limits) as client:
        tasks = [asyncio.create_task(worker(client, i, stop_at)) for i in range(workers)]
        for spec in os.environ.get("PROBES", "health_probe:0.25,est_cached_probe:1").split(","):
            k, every = spec.split(":")
            tasks.append(asyncio.create_task(prober(client, k, float(every), stop_at)))
        await asyncio.gather(*tasks)
        # wait for outstanding fire-and-forget probes (bounded)
        for _ in range(600):
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if not pending:
                break
            await asyncio.sleep(0.5)
    with open(out_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in sorted(rows, key=lambda r: float(r["t_start"])):
            w.writerow(r)
    print(json.dumps({"requests": len(rows), "out": out_path}))


asyncio.run(main())
