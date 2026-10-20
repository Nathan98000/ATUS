import time, asyncio, os
os.environ.setdefault("DATABASE_URL","postgresql://atus:atus@localhost:5434/atus")
from atus_pipeline.api.app import create_app
import httpx
events=[]
app=create_app()
async def wrapper(scope, receive, send):
    async def s(msg):
        if msg["type"]=="http.response.body" and not msg.get("more_body"):
            events.append(("body_sent", time.monotonic()))
            await asyncio.sleep(0.5)  # simulate slow client drain
            events.append(("send_returned", time.monotonic()))
        await send(msg)
    await app(scope, receive, s)
async def main():
    async with app.router.lifespan_context(app):
        pool=app.state.pool; pool.wait(10)
        orig=pool.putconn
        def put(c):
            events.append(("putconn status=%s"%c.info.transaction_status.name, time.monotonic())); return orig(c)
        pool.putconn=put
        # record tx status at end of handler via getconn wrap
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapper), base_url="http://t") as cl:
            r=await cl.get("/ready"); events.append(("client_got_%d"%r.status_code, time.monotonic()))
    t0=events[0][1]
    for e,t in events: print(f"{e:40s} +{(t-t0)*1000:.1f}ms")
asyncio.run(main())
