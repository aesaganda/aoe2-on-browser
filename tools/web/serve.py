#!/usr/bin/env python3
"""Serve the browser build for development (Phase 2):

    tools/web/serve.py [--port 8642] [--install <install-root>]

  /                 web/ (index.html, page.js) and build/web/ (aoe2.js, aoe2.wasm)
  /install/...      with --install: the install tree and install/manifest.json, for
                    the page's "Copy from this server". These are the owner's own
                    files, for their own browser, so they are served only to direct
                    loopback requests, never through a proxy such as `tailscale serve`
                    (Handler.install_allowed). Never put this on a public server.

Every response is cross-origin isolated (COOP/COEP), which the build's pthreads
(SharedArrayBuffer) need.
"""
import argparse
import http.server
import json
import os
import socketserver
import urllib.parse

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOTS = [os.path.join(REPO, "web"), os.path.join(REPO, "build", "web")]


def manifest(install):
    files = []
    for d, _subdirs, names in os.walk(install):
        for n in sorted(names):
            full = os.path.join(d, n)
            files.append([os.path.relpath(full, install).replace(os.sep, "/"), os.path.getsize(full)])
    return json.dumps({"files": files}).encode()


class Handler(http.server.SimpleHTTPRequestHandler):
    install = None
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".wasm": "application/wasm", ".js": "text/javascript", ".mjs": "text/javascript"}

    def end_headers(self):
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def install_allowed(self):
        """The install tree goes only to a browser on this machine, asking this server
        directly. Not through a proxy such as `tailscale serve`, which also connects
        from 127.0.0.1: that would hand the owner's game files to other people."""
        if not self.install:
            return False
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return False
        proxied = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded", "Via",
                   "Tailscale-User-Login", "Tailscale-User-Name", "Tailscale-Headers-Info")
        if any(self.headers.get(h) is not None for h in proxied):
            return False
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("localhost", "127.0.0.1", "::1")

    def translate_path(self, path):
        rel = urllib.parse.unquote(urllib.parse.urlsplit(path).path).lstrip("/")
        rel = os.path.normpath(rel).replace(os.sep, "/") if rel else ""
        if rel == "." or rel.startswith("../") or rel == ".." or os.path.isabs(rel):
            return ""                                   # outside every root: 404
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

    def do_GET(self):
        if urllib.parse.urlsplit(self.path).path == "/install/manifest.json":
            if not self.install_allowed():
                self.send_error(404)
                return
            body = manifest(self.install)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_HEAD(self):
        if urllib.parse.urlsplit(self.path).path == "/install/manifest.json":
            self.send_response(200 if self.install_allowed() else 404)
            self.end_headers()
            return
        super().do_HEAD()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8642)
    ap.add_argument("--install")
    a = ap.parse_args()
    Handler.install = os.path.realpath(a.install) if a.install else None
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", a.port), Handler) as s:
        print(f"http://localhost:{a.port}/" + (f"  (install: {Handler.install})" if a.install else ""), flush=True)
        s.serve_forever()


if __name__ == "__main__":
    main()
