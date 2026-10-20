"""Audit mock: serves a built ATUS Explorer dist/ with SPA fallback + a strict CSP,
and a scriptable fake /api/v1 (captured fixtures from frontend/src/test/fixtures).

Control:  POST /__control  {"rules": {"<route-key>": {"delay": s, "status": n,
          "body": "json"|"html"|"envelope", "code": "...", "headers": {...},
          "fail_times": k}}, "meta_patch": {...}}
          GET  /__log  -> request log [{t, method, path, status}]
Route keys: meta, population, activities, presets, detail, estimate, trend, compare.
"""
import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

DIST = sys.argv[1]
PORT = int(sys.argv[2])
FIX = sys.argv[3]
CSP = os.environ.get(
    "AUDIT_CSP",
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "font-src 'self'; connect-src 'self'; manifest-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'",
)
META_CACHE = os.environ.get("AUDIT_META_CACHE", "public, max-age=300")

def load(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)

FIXTURES = {
    "meta": load("meta.json"),
    "population": load("population_metadata.json"),
    "activities": load("activities.json"),
    "presets": load("activity_presets.json"),
    "detail": load("activity_0101.json"),
    "estimate": load("estimate_sleep.json"),
    "trend": load("trend_leisure.json"),
    "compare": load("compare_children.json"),
}
STATE = {"rules": {}, "meta_patch": None, "counts": {}}
LOG = []
LOCK = threading.Lock()
T0 = time.time()

def route_key(method, path):
    if path == "/api/v1/meta":
        return "meta"
    if path == "/api/v1/population/metadata":
        return "population"
    if path == "/api/v1/activities":
        return "activities"
    if path == "/api/v1/activities/presets":
        return "presets"
    if path.startswith("/api/v1/activities/"):
        return "detail"
    if method == "POST" and path.startswith("/api/v1/analysis/"):
        return path.rsplit("/", 1)[1]
    return None

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status, body, ctype, extra=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", CSP)
        if os.environ.get("AUDIT_CORS_ORIGIN"):
            # mirrors Starlette CORSMiddleware as configured in api/app.py (no expose_headers)
            self.send_header("Access-Control-Allow-Origin", os.environ["AUDIT_CORS_ORIGIN"])
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _log(self, status, key=None):
        with LOCK:
            LOG.append({"t": round(time.time() - T0, 3), "method": self.command,
                        "path": self.path.split("?")[0], "key": key, "status": status})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", os.environ.get("AUDIT_CORS_ORIGIN", ""))
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path == "/__control":
            cfg = json.loads(raw or b"{}")
            with LOCK:
                if "rules" in cfg:
                    STATE["rules"] = cfg["rules"]
                    STATE["counts"] = {}
                if "meta_patch" in cfg:
                    STATE["meta_patch"] = cfg["meta_patch"]
                if "meta_cache" in cfg:
                    STATE["meta_cache"] = cfg["meta_cache"]
                if cfg.get("clear_log"):
                    LOG.clear()
            return self._send(200, "{}", "application/json")
        return self._api()

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/__log":
            with LOCK:
                return self._send(200, json.dumps(LOG), "application/json")
        if p.startswith("/api/"):
            return self._api()
        # static + SPA fallback
        fp = os.path.normpath(os.path.join(DIST, p.lstrip("/")))
        if not fp.startswith(DIST) or not os.path.isfile(fp):
            fp = os.path.join(DIST, "index.html")
        ctype = {"js": "text/javascript", "css": "text/css", "svg": "image/svg+xml",
                 "html": "text/html; charset=utf-8"}.get(fp.rsplit(".", 1)[-1], "application/octet-stream")
        with open(fp, "rb") as f:
            body = f.read()
        self._log(200)
        return self._send(200, body, ctype)

    def _api(self):
        p = self.path.split("?")[0]
        key = route_key(self.command, p)
        if key is None:
            self._log(404, key)
            return self._send(404, json.dumps({"error": {"code": "not_found", "message": "Not Found", "details": None}}), "application/json")
        with LOCK:
            rule = dict(STATE["rules"].get(key, {}))
            n = STATE["counts"].get(key, 0) + 1
            STATE["counts"][key] = n
        with LOCK:
            LOG.append({"t": round(time.time() - T0, 3), "method": self.command, "path": p,
                        "key": key, "status": "recv", "n": n})
        fail_times = rule.get("fail_times")
        failing = rule.get("status") and (fail_times is None or n <= fail_times)
        if rule.get("delay") and (rule.get("delay_all") or failing or rule.get("status") is None):
            time.sleep(rule["delay"])
        headers = {"X-Request-ID": f"req{n:04d}"}
        if failing:
            status = rule["status"]
            headers.update(rule.get("headers", {}))
            if rule.get("body") == "html":
                self._log(status, key)
                return self._send(status, "<html><body><h1>%d upstream page</h1></body></html>" % status, "text/html", headers)
            env = {"error": {"code": rule.get("code", "x"), "message": rule.get("message", "msg"), "details": None}}
            self._log(status, key)
            return self._send(status, json.dumps(env), "application/json", headers)
        payload = FIXTURES[key]
        if key == "meta":
            payload = json.loads(json.dumps(payload))
            if STATE["meta_patch"]:
                for k, v in STATE["meta_patch"].items():
                    if v is None:
                        payload.pop(k, None)
                    elif isinstance(v, dict) and isinstance(payload.get(k), dict):
                        payload[k].update(v)
                    else:
                        payload[k] = v
            headers["Cache-Control"] = STATE.get("meta_cache") or META_CACHE
        if key in ("population", "activities", "presets", "detail"):
            headers["Cache-Control"] = "public, max-age=300"
        if key in ("estimate", "trend", "compare"):
            headers["X-Analysis-Key"] = "k" * 64
            headers["X-Cache"] = "miss"
        self._log(200, key)
        return self._send(200, json.dumps(payload), "application/json", headers)

ThreadingHTTPServer.daemon_threads = True
ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
