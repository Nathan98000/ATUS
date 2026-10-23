import asyncio, time, httpx, sys
P=sys.argv[1]
async def main():
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{P}", timeout=60, limits=httpx.Limits(max_connections=200)) as c:
        t0=time.monotonic()
        async def ready(i):
            r=await c.get("/ready"); return (time.monotonic()-t0, r.status_code, r.headers.get("retry-after"), r.text[:80])
        tasks=[asyncio.create_task(ready(i)) for i in range(45)]
        await asyncio.sleep(1)
        t=time.monotonic(); r=await c.get("/api/v1/population/metadata"); print("metadata",r.status_code,round(time.monotonic()-t,3))
        t=time.monotonic(); r=await c.get("/health"); print("health",r.status_code,round(time.monotonic()-t,3))
        res=await asyncio.gather(*tasks)
        for x in sorted(res): print("ready t=%.2f %s retry-after=%s %s"%x)
asyncio.run(main())
