"""E3b/E3c: N concurrent uncached analyses against the real app under uvicorn (UDS),
pool max_size=10, anyio default thread limiter. Probes /health (async), /api/v1/population/metadata
(sync, no DB) and /ready (sync, DB) while saturated. Usage: saturation_test.py N SLEEP_SECONDS TAG"""
import asyncio, json, sys, time, collections
import httpx
N, SLEEP, TAG = int(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
async def main():
    tr = httpx.AsyncHTTPTransport(uds="api.sock")
    lim = httpx.Limits(max_connections=1000, max_keepalive_connections=0)
    async with httpx.AsyncClient(transport=tr, base_url="http://x", timeout=120, limits=lim) as c:
        t0 = time.monotonic()
        async def analysis(i):
            body = {"activity": {"include": ["010101"], "label": f"id={TAG}-{i}"}, "years": [2023], "variance": "none"}
            s = time.monotonic()
            r = await c.post("/api/v1/analysis/estimate", json=body)
            return ("analysis", i, r.status_code, round(s - t0, 2), round(time.monotonic() - t0, 2), r.json().get("error", {}).get("code") if r.status_code != 200 else r.headers.get("x-cache"))
        async def probe(path, delay):
            await asyncio.sleep(delay)
            s = time.monotonic()
            r = await c.get(path)
            return ("probe " + path, delay, r.status_code, round(s - t0, 2), round(time.monotonic() - t0, 2), round(time.monotonic() - s, 3))
        tasks = [analysis(i) for i in range(N)]
        tasks += [probe("/health", d) for d in (0.5, 1.0, 2.0, 5.0)]
        tasks += [probe("/api/v1/population/metadata", d) for d in (1.0, 5.0)]
        tasks += [probe("/ready", d) for d in (1.0,)]
        res = await asyncio.gather(*tasks)
    an = [r for r in res if r[0] == "analysis"]
    summary = collections.Counter((r[2], r[5]) for r in an)
    print(f"N={N} sleep={SLEEP}s: outcomes {dict(summary)}")
    by_status = collections.defaultdict(list)
    for r in an: by_status[r[2]].append(r[4])
    for st, ends in sorted(by_status.items()):
        ends.sort(); print(f"  status {st}: n={len(ends)} finished at t= min {ends[0]:.2f}s  median {ends[len(ends)//2]:.2f}s  max {ends[-1]:.2f}s")
    for r in sorted(an, key=lambda r: r[4]):
        pass
    print("  completion histogram (1s buckets, status):", sorted(collections.Counter((int(r[4]), r[2]) for r in an).items()))
    for r in res:
        if r[0] != "analysis": print("  ", r[0], f"sent t={r[3]}s -> {r[2]} done t={r[4]}s latency={r[5]}s")
asyncio.run(main())
