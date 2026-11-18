#!/usr/bin/env python3
"""Serve the browser build (Phase 2):

    tools/web/serve.py [--port 8642] [--install <install-root>] [--owner <login>]

  /                 web/ (index.html, page.js) and build/web/ (aoe2.js, aoe2.wasm)
  /install/...      with --install (or an auto-detected install directory): the
                    install tree and install/manifest.json, served to all visitors
                    for automatic browser setup on first visit.
  /api/me           {"owner": true, "install": bool}: tells the page whether
                    server install files are available for auto-load.
  /health           {"ok": true}: for service monitoring.

Every response is cross-origin isolated (COOP/COEP), which the build's pthreads
(SharedArrayBuffer) need.
"""
import argparse
import gzip
import http.server
import json
import logging
import os
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse

log = logging.getLogger("serve")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOTS = [os.path.join(REPO, "web"), os.path.join(REPO, "build", "web")]

def _git_version():
    try:
        return subprocess.check_output(["git", "-C", REPO, "describe", "--always", "--dirty"],
                                       stderr=subprocess.DEVNULL, timeout=2).decode().strip()
    except Exception:
        return None

GZIP_TYPES = {"text/html", "text/javascript", "text/css", "application/json", "application/wasm"}
GZIP_MIN = 1024

CACHE_LONG = "public, max-age=86400, immutable"
CACHE_SHORT = "no-cache"
LONG_CACHE_EXT = {".wasm", ".data"}


def manifest(install):
    files = []
    for d, _subdirs, names in os.walk(install):
        for n in sorted(names):
            full = os.path.join(d, n)
            files.append([os.path.relpath(full, install).replace(os.sep, "/"), os.path.getsize(full)])
    return json.dumps({"files": files}).encode()


# 1x1 transparent PNG favicon (avoids 404 noise)
FAVICON = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
           b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
           b"\x01\x00\x00\x05\x00\x01\r\n\xb4\x00\x00\x00\x00IEND\xaeB`\x82")

PAGE_404 = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Not found</title>
<style>body{background:#111;color:#ddd;font:16px/1.5 system-ui,sans-serif;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0}
a{color:#c9a24a}</style></head>
<body><div style="text-align:center"><h1>404</h1><p>Nothing here.
<a href="/">Back to the game</a></p></div></body></html>"""


class Handler(http.server.SimpleHTTPRequestHandler):
    install = None
    owner = None
    manifest_bytes = None
    request_count = 0
    _count_lock = threading.Lock()
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".wasm": "application/wasm", ".js": "text/javascript", ".mjs": "text/javascript"}
    timeout = 120

    @classmethod
    def get_manifest(cls):
        if cls.manifest_bytes is None and cls.install:
            cls.manifest_bytes = manifest(cls.install)
        return cls.manifest_bytes

    def log_request(self, code="-", size="-"):
        elapsed = ""
        t0 = getattr(self, '_req_start', None)
        if t0 is not None:
            ms = (time.monotonic() - t0) * 1000
            elapsed = f" {ms:.0f}ms" if ms < 1000 else f" {ms / 1000:.1f}s"
        log.info('%s "%s %s" %s%s', self.address_string(),
                 self.command, self.path, code, elapsed)

    def log_error(self, fmt, *args):
        log.warning("%s %s", self.address_string(), fmt % args)

    def end_headers(self):
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if self.headers.get("Tailscale-User-Login"):
            self.send_header("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; "
                         "script-src 'self' 'wasm-unsafe-eval'; "
                         "style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' blob: data:; "
                         "connect-src 'self' ws: wss:; "
                         "worker-src 'self'; "
                         "frame-ancestors 'none'")
        p = urllib.parse.urlsplit(self.path).path
        ext = os.path.splitext(p)[1].lower()
        is_install_file = p.startswith("/install/") and p != "/install/manifest.json"
        self.send_header("Cache-Control", CACHE_LONG if (ext in LONG_CACHE_EXT or is_install_file) else CACHE_SHORT)
        super().end_headers()

    def install_allowed(self):
        return bool(self.install)

    def translate_path(self, path):
        rel = urllib.parse.unquote(urllib.parse.urlsplit(path).path).lstrip("/")
        rel = os.path.normpath(rel).replace(os.sep, "/") if rel else ""
        if rel == "." or rel.startswith("../") or rel == ".." or os.path.isabs(rel):
            return ""
        if rel.startswith("install/"):
            if not self.install_allowed():
                return ""
            return self.inside(self.install, rel[len("install/"):])
        rel = rel or "index.html"
        for r in ROOTS:
            p = self.inside(r, rel)
            if p and os.path.exists(p):
                return p
        return self.inside(ROOTS[0], rel)

    @staticmethod
    def inside(root, rel):
        rr = os.path.realpath(root)
        p = os.path.realpath(os.path.join(rr, rel))
        return p if p == rr or p.startswith(rr + os.sep) else ""

    def _json_response(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        body = self._maybe_gzip(body, "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _maybe_gzip(self, body, content_type):
        if len(body) < GZIP_MIN:
            return body
        if content_type not in GZIP_TYPES:
            return body
        ae = self.headers.get("Accept-Encoding", "")
        if "gzip" not in ae:
            return body
        compressed = gzip.compress(body, compresslevel=6)
        if len(compressed) >= len(body):
            return body
        self.send_header("Content-Encoding", "gzip")
        return compressed

    def _send_file_with_etag(self, fpath, content_type):
        try:
            stat = os.stat(fpath)
        except OSError:
            self.send_error(404)
            return
        etag = '"%x-%x"' % (int(stat.st_mtime), stat.st_size)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.end_headers()
            return
        with open(fpath, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("ETag", etag)
        body = self._maybe_gzip(body, content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._req_start = time.monotonic()
        with self._count_lock:
            Handler.request_count += 1
        path = urllib.parse.urlsplit(self.path).path

        if path == "/health":
            return self._json_response({"ok": True, "requests": Handler.request_count})

        if path == "/api/version":
            v = _git_version()
            return self._json_response({"version": v} if v else {"version": None})

        if path == "/api/me":
            return self._json_response({"owner": True, "install": bool(self.install)})

        if path == "/favicon.ico":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(FAVICON)))
            self.send_header("Cache-Control", "public, max-age=604800")
            self.end_headers()
            self.wfile.write(FAVICON)
            return

        if path == "/install/manifest.json":
            if not self.install_allowed():
                self.send_error(404)
                return
            body = self.get_manifest()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            body = self._maybe_gzip(body, "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ETag support for large static files (.wasm, .js)
        fpath = self.translate_path(self.path)
        if fpath and os.path.isfile(fpath):
            ext = os.path.splitext(fpath)[1].lower()
            ct = self.extensions_map.get(ext, "application/octet-stream")
            if ext in (".wasm", ".js", ".mjs", ".html", ".css", ".json"):
                return self._send_file_with_etag(fpath, ct)

        super().do_GET()

    def do_HEAD(self):
        self._req_start = time.monotonic()
        if urllib.parse.urlsplit(self.path).path == "/install/manifest.json":
            self.send_response(200 if self.install_allowed() else 404)
            self.end_headers()
            return
        super().do_HEAD()

    def send_error(self, code, message=None, explain=None):
        if code == 404:
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE_404)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(PAGE_404)
            return
        super().send_error(code, message, explain)

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8642)
    ap.add_argument("--install")
    ap.add_argument("--owner", help="Tailscale login (email) of the owner")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stderr)

    install = a.install
    if not install:
        for cand in ("/root/aoe2-recomp-work/install",
                     os.path.expanduser("~/aoe2-recomp-work/install"),
                     os.path.expanduser("~/aoe2-install")):
            if os.path.isdir(os.path.join(cand, "age2_x1")):
                install = cand
                break
    Handler.install = os.path.realpath(install) if install else None
    Handler.owner = a.owner.lower() if a.owner else None
    Handler.manifest_bytes = manifest(Handler.install) if Handler.install and os.path.isdir(Handler.install) else None

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    server = socketserver.ThreadingTCPServer(("127.0.0.1", a.port), Handler)
    server.daemon_threads = True
    server.request_queue_size = 32
    server.timeout = 120

    def shutdown(sig, frame):
        log.info("shutting down (signal %d)", sig)
        # shutdown() must run from a different thread than serve_forever()
        threading.Thread(target=server.shutdown, daemon=True).start()
        # Hard exit if graceful shutdown hangs (e.g. a slow transfer)
        threading.Timer(5.0, lambda: os._exit(0)).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    info = f"http://localhost:{a.port}/"
    if Handler.install:
        info += f"  (install: {Handler.install}"
        if Handler.owner:
            info += f", owner: {Handler.owner}"
        info += ")"
    log.info(info)
    server.serve_forever()
    server.server_close()
    log.info("stopped")


if __name__ == "__main__":
    main()
