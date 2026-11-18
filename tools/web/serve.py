#!/usr/bin/env python3
"""Serve the browser build for development (Phase 2):

    tools/web/serve.py [--port 8642] [--install <install-root>]

  /                 web/ (index.html, page.js) and build/web/ (aoe2.js, aoe2.wasm)
  /install/...      with --install: the install tree and install/manifest.json, for
                    the page's "Copy from this server" (the owner's own files, to
                    their own browser; never put this on a public server)

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

    def translate_path(self, path):
        rel = urllib.parse.unquote(urllib.parse.urlsplit(path).path).lstrip("/")
        if rel.startswith("install/") and self.install:
            return os.path.join(self.install, rel[len("install/"):])
        rel = rel or "index.html"
        for r in ROOTS:
            p = os.path.join(r, rel)
            if os.path.exists(p):
                return p
        return os.path.join(ROOTS[0], rel)

    def do_GET(self):
        if self.install and urllib.parse.urlsplit(self.path).path == "/install/manifest.json":
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
            self.send_response(200 if self.install else 404)
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
