"""Slow-client probe: does uvicorn close connections that send headers or body very slowly?"""
import socket, time, sys
HOST, PORT = "127.0.0.1", int(sys.argv[1])
DURATION = float(sys.argv[2])
def conn():
    s = socket.create_connection((HOST, PORT)); s.settimeout(0.2); return s
def alive(s):
    try:
        d = s.recv(4096)
        return (False, d[:120]) if d == b"" else (True, d[:120])
    except socket.timeout:
        return (True, None)
    except OSError as e:
        return (False, repr(e))
# A: partial request line/headers, one header byte every 2 s
a = conn(); a.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n")
# B: full headers announcing a 1000-byte body, then 1 body byte every 2 s
b = conn(); b.sendall(b"POST /api/v1/analysis/estimate HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n{")
# C: idle connection after nothing sent at all
c = conn()
t0 = time.time(); log = []
while time.time() - t0 < DURATION:
    time.sleep(2)
    el = round(time.time() - t0, 1)
    for name, s, payload in (("A_slow_headers", a, b"X-a: b\r\n"), ("B_slow_body", b, b" "), ("C_idle_no_bytes", c, None)):
        ok, info = alive(s)
        if ok and payload:
            try: s.sendall(payload)
            except OSError as e: ok, info = False, repr(e)
        log.append(f"t={el}s {name} open={ok} recv={info!r}")
for line in log: print(line)
