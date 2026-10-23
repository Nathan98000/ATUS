import asyncio, socket, time, sys, os, uvicorn
SOCK = sys.argv[1]
async def app(scope, receive, send):
    if scope["type"] != "http": return
    more = True
    while more:
        m = await receive(); more = m.get("more_body", False)
    await send({"type":"http.response.start","status":200,"headers":[]})
    await send({"type":"http.response.body","body":b"ok"})
async def main():
    cfg = uvicorn.Config(app, uds=SOCK, log_level="warning")  # defaults, like cli.py
    srv = uvicorn.Server(cfg)
    task = asyncio.create_task(srv.serve())
    while not srv.started: await asyncio.sleep(0.05)
    loop = asyncio.get_running_loop()
    def mk():
        s = socket.socket(socket.AF_UNIX); s.connect(SOCK); s.setblocking(False); return s
    A, B, C = mk(), mk(), mk()
    A.send(b"GET / HTTP/1.1\r\nHost: x\r\n")
    B.send(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 1000\r\n\r\n")
    t0 = time.time()
    while time.time() - t0 < float(sys.argv[2]):
        await asyncio.sleep(5)
        A.send(b"X-a: b\r\n"); B.send(b"z")
        st = []
        for n, s in (("A",A),("B",B),("C",C)):
            try:
                d = s.recv(100); st.append(f"{n}:closed" if d == b"" else f"{n}:data")
            except BlockingIOError: st.append(f"{n}:open")
            except Exception as e: st.append(f"{n}:{type(e).__name__}")
        print(f"t={time.time()-t0:.1f} {' '.join(st)} server_conns={len(srv.server_state.connections)}", flush=True)
    srv.should_exit = True; srv.force_exit = True
    await task
asyncio.run(main())
