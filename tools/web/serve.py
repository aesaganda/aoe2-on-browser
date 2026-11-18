#!/usr/bin/env python3
"""Serve the browser build (Phase 2):

    tools/web/serve.py [--port 8642] [--install <install-root>] [--owner <login>]

  /                 web/ (index.html, page.js) and build/web/ (aoe2.js, aoe2.wasm)
  /install/...      with --install: the install tree and install/manifest.json, for
                    the page's auto-copy on the owner's first visit. Served only to:
                    - direct loopback requests (the owner's own machine, no proxy);
                    - with --owner, the owner's Tailscale identity through
                      `tailscale serve` (Tailscale-User-Login header).
                    Never put this on a public server.
  /api/me           {"owner": bool, "install": bool}: lets the page know whether the
                    visitor is the owner and whether auto-copy is available.
  /health           {"ok": true}: for service monitoring.

Every response is cross-origin isolated (COOP/COEP), which the build's pthreads
(SharedArrayBuffer) need.
"""
import argparse
import gzip
import hashlib
import http.server
import json
import logging
import os
import signal
import socketserver
import sys
import urllib.parse

log = logging.getLogger("serve")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOTS = [os.path.join(REPO, "web"), os.path.join(REPO, "build", "web")]

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


class Handler(http.server.SimpleHTTPRequestHandler):
    install = None
    owner = None
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".wasm": "application/wasm", ".js": "text/javascript", ".mjs": "text/javascript"}

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def log_error(self, fmt, *args):
        log.warning("%s %s", self.address_string(), fmt % args)

    def end_headers(self):
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        p = urllib.parse.urlsplit(self.path).path
        ext = os.path.splitext(p)[1].lower()
        self.send_header("Cache-Control", CACHE_LONG if ext in LONG_CACHE_EXT else CACHE_SHORT)
        super().end_headers()

    def _direct_loopback(self):
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return False
        proxied = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded", "Via",
                   "Tailscale-User-Login", "Tailscale-User-Name", "Tailscale-Headers-Info")
        if any(self.headers.get(h) is not None for h in proxied):
            return False
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("localhost", "127.0.0.1", "::1")

    def _is_owner(self):
        if self._direct_loopback():
            return True
        if not self.owner:
            return False
        login = self.headers.get("Tailscale-User-Login")
        return login is not None and login.lower() == self.owner

    def install_allowed(self):
        if not self.install:
            return False
        return self._is_owner()

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
        path = urllib.parse.urlsplit(self.path).path

        if path == "/health":
            return self._json_response({"ok": True})

        if path == "/api/me":
            is_owner = self._is_owner()
            return self._json_response({"owner": is_owner, "install": bool(self.install) and is_owner})

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
            body = manifest(self.install)
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
        if urllib.parse.urlsplit(self.path).path == "/install/manifest.json":
            self.send_response(200 if self.install_allowed() else 404)
            self.end_headers()
            return
        super().do_HEAD()

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

    Handler.install = os.path.realpath(a.install) if a.install else None
    Handler.owner = a.owner.lower() if a.owner else None

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    server = socketserver.ThreadingTCPServer(("127.0.0.1", a.port), Handler)
    server.daemon_threads = True

    def shutdown(sig, frame):
        log.info("shutting down (signal %d)", sig)
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    info = f"http://localhost:{a.port}/"
    if a.install:
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
