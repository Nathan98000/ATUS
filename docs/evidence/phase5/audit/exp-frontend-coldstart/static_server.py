#!/usr/bin/env python3
"""Static server with SPA fallback for the scratch production build.
Listens on 127.0.0.1:PORT and [::1]:PORT (loopback only) so the same bundle is
reachable from three distinct origins (127.0.0.1, localhost, [::1])."""
import os, socket, sys, threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from functools import partial

ROOT = sys.argv[1]
PORT = int(sys.argv[2])

class H(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def send_head(self):
        path = self.translate_path(self.path)
        if not os.path.exists(path) or (os.path.isdir(path) and not os.path.exists(os.path.join(path, 'index.html'))):
            self.path = '/index.html'  # SPA fallback
        return super().send_head()
    def end_headers(self):
        if self.path.endswith('.html') or self.path == '/':
            self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

class S4(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
class S6(S4):
    address_family = socket.AF_INET6

handler = partial(H, directory=ROOT)
servers = [S4(('127.0.0.1', PORT), handler), S6(('::1', PORT), handler)]
for s in servers[1:]:
    threading.Thread(target=s.serve_forever, daemon=True).start()
print(f'static SPA server on 127.0.0.1:{PORT} and [::1]:{PORT} root={ROOT}', flush=True)
servers[0].serve_forever()
