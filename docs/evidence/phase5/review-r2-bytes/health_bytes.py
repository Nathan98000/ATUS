"""Byte count (not timed) of the production-mode API's /health answer, served
with the options `atus api` passes to uvicorn (server_header off). No database
is contacted: /health never touches it, and the URL points at a closed port.
Each line's size is that of the line as sent; the request ID and the
Server-Timing durations are masked in the printout, so no timing is recorded."""
import os
import re
import secrets
import socket
import threading
import time

PORT = 9520
EDGE = secrets.token_hex(32)
env = {"ATUS_ENV": "production",
       "ATUS_DATABASE_URL": "postgresql://atus_api:x@127.0.0.1:9/atus",
       "ATUS_DATABASE_TLS": "local-stack-only", "ATUS_EDGE_SECRETS": EDGE,
       "ATUS_EXPECTED_DATA_VERSION": "x", "ATUS_LOG_LEVEL": "WARNING",
       "ATUS_BUILD_SHA": "0" * 40}
os.environ.update(env)
import uvicorn  # noqa: E402
from atus_pipeline.api.app import create_app  # noqa: E402
from atus_pipeline.api.serve import uvicorn_options  # noqa: E402
from atus_pipeline.config import load_settings  # noqa: E402

settings = load_settings(env)
options = uvicorn_options(settings, host="127.0.0.1", port=PORT)
for key in ("app", "factory", "reload", "workers"):
    options.pop(key)
srv = uvicorn.Server(uvicorn.Config(create_app(settings), **options))
t = threading.Thread(target=srv.run, daemon=True)
t.start()
while not srv.started:
    time.sleep(0.05)
for label, extra in (("as the Worker's fetch would (keep-alive, gzip accepted)", ""),
                     ("with the edge secret", f"X-ATUS-Edge-Auth: {EDGE}\r\n")):
    req = (f"GET /health HTTP/1.1\r\nHost: x\r\nAccept-Encoding: gzip\r\n{extra}\r\n").encode()
    s = socket.create_connection(("127.0.0.1", PORT))
    s.sendall(req)
    raw = b""
    while b"\r\n\r\n" not in raw:
        raw += s.recv(65536)
    head, _, body = raw.partition(b"\r\n\r\n")
    length = int([line for line in head.split(b"\r\n")
                  if line.lower().startswith(b"content-length")][0].split(b":")[1])
    while len(body) < length:
        body += s.recv(65536)
    s.close()
    print(f"--- {label}")
    for line in head.split(b"\r\n"):
        name = line.split(b":", 1)[0]
        shown = name + b": <hex>" if name.lower() == b"x-request-id" else line
        if name.lower() == b"server-timing":
            shown = re.sub(rb"dur=[0-9.]+", b"dur=*", line)
        print(len(line) + 2, shown.decode())
    print(f"TOTAL {len(head) + 4 + len(body)} B (headers {len(head) + 4} + body {len(body)})")
srv.should_exit = True
t.join(5)
