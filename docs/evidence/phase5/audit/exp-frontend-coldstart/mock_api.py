#!/usr/bin/env python3
"""Mock of the ATUS Phase 3 API for frontend cold-start / failure-mode experiments.

Replays the captured fixtures in frontend/src/test/fixtures (read-only) and
injects failure scenarios. State (counters, wake times) is kept PER ORIGIN so
several pages, each served from a different origin, can run concurrently.

Control:   GET /__scenario?name=<scenario>[&n=N][&delay=S]   (resets state)
           GET /__log                                         (JSON list)
Log:       every request appended as JSONL to $MOCK_LOG.
"""
import copy
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

FIXTURES = "$REPO/frontend/src/test/fixtures"
PORT = int(os.environ.get("MOCK_PORT", "8207"))
LOG_PATH = os.environ.get("MOCK_LOG", "mock_requests.jsonl")
ALLOWED_ORIGINS = {
    "http://localhost:4182",
    "http://127.0.0.1:4182",
    "http://[::1]:4182",
}


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


FX = {
    "meta": load("meta.json"),
    "population": load("population_metadata.json"),
    "activities": load("activities.json"),
    "presets": load("activity_presets.json"),
    "activity_detail": load("activity_0101.json"),
    "estimate": load("estimate_sleep.json"),
    "trend": load("trend_leisure.json"),
    "compare": load("compare_children.json"),
}

CF_502 = """<!DOCTYPE html>
<html lang="en-US"><head><title>atus-api.example | 502: Bad gateway</title></head>
<body><div id="cf-error-details"><h1>Bad gateway</h1><span>Error code 502</span>
<p>The web server reported a bad gateway error.</p><p>Cloudflare Ray ID: 8d0000000000abcd</p></div></body></html>
"""
CF_504 = """<!DOCTYPE html>
<html lang="en-US"><head><title>atus-api.example | 504: Gateway time-out</title></head>
<body><div id="cf-error-details"><h1>Gateway time-out</h1><span>Error code 504</span>
<p>The web server reported a gateway time-out error.</p><p>Cloudflare Ray ID: 8d0000000000abce</p></div></body></html>
"""
CF_1027 = """<!DOCTYPE html>
<html lang="en-US"><head><title>Error 1027 | This website has been temporarily rate limited</title></head>
<body><h1>Error 1027</h1><h2>This website has been temporarily rate limited</h2></body></html>
"""
PLAIN_503 = "Service Unavailable\n"
LOADING_HTML = """<!DOCTYPE html><html><head><title>Service waking up</title></head>
<body><h1>Your service is waking up</h1><p>This may take a minute.</p></body></html>
"""

STATE_LOCK = threading.Lock()
STATE = {"name": "baseline", "n": 2, "delay": 60.0, "origins": {}}
LOG = []


def origin_state(origin):
    with STATE_LOCK:
        st = STATE["origins"].get(origin)
        if st is None:
            st = {"first": time.time(), "counts": {}}
            STATE["origins"][origin] = st
        return st


def bump(origin, key):
    st = origin_state(origin)
    with STATE_LOCK:
        st["counts"][key] = st["counts"].get(key, 0) + 1
        return st["counts"][key]


def envelope(code, message, details=None):
    return {"error": {"code": code, "message": message, "details": details}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mock-atus/1"

    def log_message(self, fmt, *args):  # quiet stderr
        pass

    # ---- helpers -------------------------------------------------------
    def _cors(self, enabled=True, expose=True):
        origin = self.headers.get("Origin")
        if not enabled or not origin or origin not in ALLOWED_ORIGINS:
            return {}
        h = {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
        if expose:
            h["Access-Control-Expose-Headers"] = "X-Request-ID, X-Analysis-Key, X-Cache"
        return h

    def _send(self, status, body, ctype="application/json", headers=None, cors=True, expose=True):
        if isinstance(body, (dict, list)):
            raw = json.dumps(body).encode()
        else:
            raw = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Request-ID", uuid.uuid4().hex[:16])
        for k, v in self._cors(cors, expose).items():
            self.send_header(k, v)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
        return status

    def _record(self, status, t0, note=""):
        entry = {
            "t_start": round(t0, 3),
            "t_end": round(time.time(), 3),
            "scenario": STATE["name"],
            "origin": self.headers.get("Origin"),
            "method": self.command,
            "path": self.path,
            "status": status,
            "note": note,
        }
        with STATE_LOCK:
            LOG.append(entry)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    # ---- control -------------------------------------------------------
    def _control(self, url):
        q = parse_qs(url.query)
        if url.path == "/__scenario":
            with STATE_LOCK:
                STATE["name"] = q.get("name", ["baseline"])[0]
                STATE["n"] = int(q.get("n", ["2"])[0])
                STATE["delay"] = float(q.get("delay", ["60"])[0])
                STATE["origins"] = {}
            return self._send(200, {"ok": True, "scenario": STATE["name"], "n": STATE["n"], "delay": STATE["delay"]})
        if url.path == "/__log":
            with STATE_LOCK:
                data = list(LOG)
            return self._send(200, data)
        return self._send(404, envelope("not_found", "unknown control"))

    # ---- normal fixture responses -------------------------------------
    def _normal(self, path, realcors=False):
        expose = not realcors
        p = path
        cache = {"Cache-Control": "public, max-age=300"}
        if p == "/api/v1/meta":
            return self._send(200, FX["meta"], headers=cache, expose=expose)
        if p == "/api/v1/population/metadata":
            return self._send(200, FX["population"], headers=cache, expose=expose)
        if p == "/api/v1/activities":
            return self._send(200, FX["activities"], headers=cache, expose=expose)
        if p == "/api/v1/activities/presets":
            return self._send(200, FX["presets"], headers=cache, expose=expose)
        if p.startswith("/api/v1/activities/"):
            return self._send(200, FX["activity_detail"], headers=cache, expose=expose)
        if p.startswith("/api/v1/analysis/"):
            op = p.rsplit("/", 1)[-1]
            if op in ("estimate", "trend", "compare"):
                return self._send(
                    200, FX[op], expose=expose,
                    headers={"X-Analysis-Key": "a" * 64, "X-Cache": "miss"},
                )
        if p in ("/health", "/ready"):
            return self._send(200, {"status": "ok"}, expose=expose)
        return self._send(404, envelope("not_found", "Not Found"), expose=expose)

    # ---- scenario dispatch --------------------------------------------
    def _handle(self):
        t0 = time.time()
        url = urlparse(self.path)
        if url.path.startswith("/__"):
            self._control(url)
            return
        origin = self.headers.get("Origin") or "none"
        name = STATE["name"]
        n = STATE["n"]
        delay = STATE["delay"]
        path = url.path
        is_meta = path == "/api/v1/meta" and self.command == "GET"
        is_analysis = path.startswith("/api/v1/analysis/") and self.command == "POST"
        note = ""

        # Preflight handling (POST with JSON body triggers one).
        if self.command == "OPTIONS":
            st = origin_state(origin)
            if name == "a2_spinup60":
                wake = st["first"] + delay
                if time.time() < wake:
                    time.sleep(max(0.0, wake - time.time()))
                    note = "held-until-wake"
            self.send_response(200)
            for k, v in self._cors(True, False).items():
                self.send_header(k, v)
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()
            self._record(200, t0, note)
            return

        status = None
        if name == "baseline":
            status = self._normal(path)
        elif name == "baseline_realcors":
            status = self._normal(path, realcors=True)
        elif name == "a_meta_delay60":
            if is_meta:
                time.sleep(delay)
                note = f"delayed {delay}s"
            status = self._normal(path)
        elif name == "a2_spinup60":
            st = origin_state(origin)
            wake = st["first"] + delay
            if time.time() < wake:
                time.sleep(max(0.0, wake - time.time()))
                note = "held-until-wake"
            status = self._normal(path)
        elif name == "b_meta_delay60_503_always":
            if is_meta:
                time.sleep(delay)
                status = self._send(503, envelope(
                    "database_unavailable",
                    "The analytical database is currently unavailable. Try again shortly."))
                note = f"delayed {delay}s then 503"
            else:
                status = self._normal(path)
        elif name == "b2_meta_delay60_503_once":
            if is_meta:
                k = bump(origin, "meta")
                if k == 1:
                    time.sleep(delay)
                    status = self._send(503, envelope(
                        "database_unavailable",
                        "The analytical database is currently unavailable. Try again shortly."))
                    note = f"#{k} delayed {delay}s then 503"
                else:
                    status = self._normal(path)
                    note = f"#{k}"
            else:
                status = self._normal(path)
        elif name in ("c_v2_same_shape", "c2_v2_changed_shape"):
            if is_meta:
                m = copy.deepcopy(FX["meta"])
                m["api_version"] = "v2"
                if name == "c2_v2_changed_shape":
                    # A plausible incompatible v2: years moved/renamed.
                    m["data"]["year_range"] = {"first": 2003, "last": 2025}
                    del m["data"]["years"]
                status = self._send(200, m, headers={"Cache-Control": "public, max-age=300"})
                note = "api_version=v2"
            else:
                status = self._normal(path)
        elif name == "c3_v1_removed_404":
            if path.startswith("/api/v1/"):
                status = self._send(404, envelope("not_found", "Not Found"))
            else:
                status = self._normal(path)
        elif name == "d_503_firstN_plain":
            # Per (origin, path): first N requests -> 503 with a non-JSON body
            # (a proxy/edge answering while the API wakes), then normal.
            k = bump(origin, self.command + path)
            if k <= n and path.startswith("/api/v1/"):
                status = self._send(503, PLAIN_503, ctype="text/plain")
                note = f"#{k} 503 plain"
            else:
                status = self._normal(path)
                note = f"#{k}"
        elif name == "d_503_firstN_json":
            k = bump(origin, self.command + path)
            if k <= n and path.startswith("/api/v1/"):
                status = self._send(503, envelope(
                    "database_unavailable",
                    "The analytical database is currently unavailable. Try again shortly."))
                note = f"#{k} 503 json"
            else:
                status = self._normal(path)
                note = f"#{k}"
        elif name == "d_503_for60s_plain":
            st = origin_state(origin)
            if time.time() < st["first"] + delay and path.startswith("/api/v1/"):
                status = self._send(503, PLAIN_503, ctype="text/plain")
                note = "503 while waking"
            else:
                status = self._normal(path)
        elif name in ("e_502_html", "e_502_html_nocors", "e_504_html", "e_429_json",
                      "e_1027_html", "e_429_html_nocors"):
            target = path.startswith("/api/v1/")
            if target:
                if name == "e_502_html":
                    status = self._send(502, CF_502, ctype="text/html; charset=UTF-8")
                elif name == "e_502_html_nocors":
                    status = self._send(502, CF_502, ctype="text/html; charset=UTF-8", cors=False)
                elif name == "e_504_html":
                    status = self._send(504, CF_504, ctype="text/html; charset=UTF-8")
                elif name == "e_429_json":
                    status = self._send(429, envelope(
                        "rate_limited", "Too many requests. Try again in 30 seconds.",
                        {"retry_after_seconds": 30}), headers={"Retry-After": "30"})
                elif name == "e_1027_html":
                    status = self._send(429, CF_1027, ctype="text/html; charset=UTF-8",
                                        headers={"Retry-After": "30"})
                elif name == "e_429_html_nocors":
                    status = self._send(429, CF_1027, ctype="text/html; charset=UTF-8",
                                        headers={"Retry-After": "30"}, cors=False)
            else:
                status = self._normal(path)
        elif name in ("g_html200_for60s", "g_html200_for60s_nocors"):
            # A host-provided "service is waking up" HTML page served with 200
            # (Render documents a loading page for connecting browsers; whether
            # fetch() calls get it is undocumented) for the first `delay` s.
            st = origin_state(origin)
            if time.time() < st["first"] + delay and path.startswith("/api/v1/"):
                status = self._send(
                    200, LOADING_HTML, ctype="text/html; charset=utf-8",
                    cors=(name == "g_html200_for60s"))
                note = "200 html loading page"
            else:
                status = self._normal(path)
        elif name.startswith("f_analysis_"):
            # Metadata fine; only POST /analysis/* fails.
            if is_analysis:
                kind = name[len("f_analysis_"):]
                if kind == "502_html":
                    status = self._send(502, CF_502, ctype="text/html; charset=UTF-8")
                elif kind == "504_html":
                    status = self._send(504, CF_504, ctype="text/html; charset=UTF-8")
                elif kind == "429_json":
                    status = self._send(429, envelope(
                        "rate_limited", "Too many requests. Try again in 30 seconds.",
                        {"retry_after_seconds": 30}), headers={"Retry-After": "30"})
                elif kind == "1027_html":
                    status = self._send(429, CF_1027, ctype="text/html; charset=UTF-8",
                                        headers={"Retry-After": "30"})
                elif kind == "503_pooltimeout":
                    status = self._send(503, envelope(
                        "database_unavailable",
                        "The analytical database is currently unavailable. Try again shortly."))
                elif kind == "502_html_nocors":
                    status = self._send(502, CF_502, ctype="text/html; charset=UTF-8", cors=False)
                else:
                    status = self._normal(path)
            else:
                status = self._normal(path)
        else:
            status = self._normal(path)
        self._record(status, t0, note)

    def do_GET(self):
        self._handle()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._handle()

    def do_OPTIONS(self):
        self._handle()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    srv = Server(("127.0.0.1", PORT), Handler)
    print(f"mock ATUS API on 127.0.0.1:{PORT}, log {LOG_PATH}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
