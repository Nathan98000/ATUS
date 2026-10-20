import asyncio, json, subprocess, sys, time, httpx
PID=int(sys.argv[1]); SIZES=[int(x) for x in sys.argv[2].split(",")]; CONC=int(sys.argv[3]) if len(sys.argv)>3 else 1
def rss(): return int(subprocess.run(["ps","-o","rss=","-p",str(PID)],capture_output=True,text=True).stdout.strip())/1024
async def main():
    tr=httpx.AsyncHTTPTransport(uds="api.sock")
    async with httpx.AsyncClient(transport=tr,base_url="http://x",timeout=300) as c:
        for MB in SIZES:
            body=json.dumps({"activity":{"include":["010101"],"label":"x"*(MB*1024*1024)},"years":[2023]}).encode()
            peak=[0.0]; stop=False
            async def samp():
                while not stop:
                    peak[0]=max(peak[0],rss()); await asyncio.sleep(0.02)
            before=rss(); t=asyncio.create_task(samp()); s=time.monotonic()
            rs=await asyncio.gather(*[c.post("/api/v1/analysis/estimate",content=body,headers={"content-type":"application/json"}) for _ in range(CONC)])
            el=time.monotonic()-s; await asyncio.sleep(0.5); stop=True; await t
            print(f"MB={MB} conc={CONC} bytes={len(body)} status={[r.status_code for r in rs]} resp_len={len(rs[0].content)} el={el:.2f}s rss_before={before:.1f} peak={peak[0]:.1f} after={rss():.1f}",flush=True)
asyncio.run(main())
