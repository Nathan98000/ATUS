"""E6: request-body size. Sends one oversized JSON body (label far beyond max_length=120)
and samples server RSS and /health latency while it is processed."""
import asyncio, json, subprocess, sys, time
import httpx
PID = int(sys.argv[1]); MB = int(sys.argv[2])
def rss_mb():
    return int(subprocess.run(["ps", "-o", "rss=", "-p", str(PID)], capture_output=True, text=True).stdout.strip()) / 1024
async def main():
    tr = httpx.AsyncHTTPTransport(uds="api.sock")
    async with httpx.AsyncClient(transport=tr, base_url="http://x", timeout=300) as c:
        body = json.dumps({"activity": {"include": ["010101"], "label": "x" * (MB * 1024 * 1024)}, "years": [2023]}).encode()
        print(f"body bytes={len(body)}  server RSS before={rss_mb():.1f} MB")
        peak = [0.0]; health = []
        async def sampler():
            while True:
                peak[0] = max(peak[0], rss_mb())
                s = time.monotonic(); await c.get("/health"); health.append(time.monotonic() - s)
                await asyncio.sleep(0.05)
        t = asyncio.create_task(sampler())
        s = time.monotonic()
        r = await c.post("/api/v1/analysis/estimate", content=body, headers={"content-type": "application/json"})
        el = time.monotonic() - s
        await asyncio.sleep(0.3); t.cancel()
        print(f"status={r.status_code} elapsed={el:.2f}s body={r.text[:160]}")
        print(f"peak sampled server RSS={peak[0]:.1f} MB; after={rss_mb():.1f} MB; max /health latency during request={max(health):.3f}s (n={len(health)})")
asyncio.run(main())
