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
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("serve")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOTS = [os.path.join(REPO, "web"), os.path.join(REPO, "build", "web")]


def _git_version(build_dir=None):
    bdir = build_dir or os.path.join(REPO, "build", "web")
    ver = os.environ.get("AOE2_VERSION", "").strip() or None
    if not ver:
        try:
            ver = subprocess.check_output(["git", "-c", "safe.directory=*", "-C", REPO, "describe", "--always", "--dirty"],
                                          stderr=subprocess.DEVNULL, timeout=2).decode().strip()
        except Exception:
            try:
                head_path = os.path.join(REPO, ".git", "HEAD")
                with open(head_path, "r", encoding="utf-8") as f:
                    head = f.read().strip()
                if head.startswith("ref: "):
                    ref_path = os.path.join(REPO, ".git", *head[5:].split("/"))
                    with open(ref_path, "r", encoding="utf-8") as rf:
                        ver = rf.read().strip()[:7]
                else:
                    ver = head[:7]
            except Exception:
                ver = None
    wasm_path = os.path.join(bdir, "aoe2.wasm")
    try:
        st = os.stat(wasm_path)
        wasm_mtime = f"{int(st.st_mtime):x}"
        wasm_size = int(st.st_size)
        full_ver = f"{ver}-{wasm_mtime}" if ver else f"{wasm_mtime}-{wasm_size:x}"
        return full_ver, wasm_size
    except OSError:
        return ver, 0

GZIP_TYPES = {"text/html", "text/javascript", "text/css", "application/json", "application/wasm", "image/svg+xml"}
GZIP_MIN = 1024

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".wasm": "application/wasm",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".tar": "application/x-tar",
}

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


def _skip_bundle_path(rel):
    p = rel.lower()
    return (p.startswith("cdrom/") or p.endswith(".avi") or p.endswith(".cab")
            or p == "cdrom.label" or p.split("/")[-1] == ".complete")


def bundle_index(install):
    entries = []
    for d, subdirs, names in os.walk(install):
        subdirs.sort()
        for n in sorted(names):
            full = os.path.join(d, n)
            rel = os.path.relpath(full, install).replace(os.sep, "/")
            if _skip_bundle_path(rel):
                continue
            st = os.stat(full)
            entries.append((rel, full, st.st_size, int(st.st_mtime)))
    return entries


def bundle_size(entries):
    return sum(512 + ((size + 511) & ~511) for _, _, size, _ in entries) + 1024


def _tar_header(rel_path, size, mtime):
    raw = rel_path.encode("utf-8")
    prefix = b""
    name = raw
    if len(raw) > 100:
        slash = raw.rfind(b"/", 0, 156)
        if slash <= 0 or len(raw) - slash - 1 > 100:
            raise ValueError(f"path too long for ustar: {rel_path}")
        prefix = raw[:slash]
        name = raw[slash + 1:]
    hdr = bytearray(512)
    hdr[0:len(name)] = name
    hdr[100:108] = b"0000644\0"
    hdr[108:116] = b"0000000\0"
    hdr[116:124] = b"0000000\0"
    hdr[124:136] = f"{size:011o}\0".encode("ascii")
    hdr[136:148] = f"{mtime & 0o77777777777:011o}\0".encode("ascii")
    hdr[148:156] = b"        "
    hdr[156:157] = b"0"
    hdr[257:263] = b"ustar\0"
    hdr[263:265] = b"00"
    if prefix:
        hdr[345:345 + len(prefix)] = prefix
    chk = sum(hdr)
    hdr[148:156] = f"{chk:06o}\0 ".encode("ascii")
    return bytes(hdr)


GZ_CACHE_MIN = 1 << 20
STREAM_CHUNK = 262144
_gz_locks_guard = threading.Lock()
_gz_locks = {}


def _ensure_gz(fpath, mtime, cache_dir=None):
    with _gz_locks_guard:
        lock = _gz_locks.get(fpath)
        if lock is None:
            lock = threading.Lock()
            _gz_locks[fpath] = lock

    def _try_existing(candidate):
        try:
            st = os.stat(candidate)
            if st.st_mtime >= mtime:
                return candidate
        except OSError:
            pass
        return None

    def _write_gz(dst_path):
        tmp = f"{dst_path}.tmp.{os.getpid()}.{threading.get_ident()}"
        try:
            with open(fpath, "rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
                while True:
                    buf = src.read(STREAM_CHUNK)
                    if not buf:
                        break
                    dst.write(buf)
            os.utime(tmp, (mtime, mtime))
            os.replace(tmp, dst_path)
            return dst_path
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return None

    with lock:
        primary_gz = fpath + ".gz"
        hit = _try_existing(primary_gz)
        if hit:
            return hit
        written = _write_gz(primary_gz)
        if written:
            return written

        cdir = cache_dir or "/tmp/aoe2-cache"
        try:
            os.makedirs(cdir, exist_ok=True)
        except OSError:
            return None
        fallback_gz = os.path.join(cdir, f"{os.path.basename(fpath)}.{int(mtime):x}.gz")
        hit = _try_existing(fallback_gz)
        if hit:
            return hit
        return _write_gz(fallback_gz)


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
    protocol_version = "HTTP/1.1"
    install = None
    owner = None
    manifest_bytes = None
    bundle_entries = None
    bundle_total = 0
    bundle_etag = None
    relay_url = "http://127.0.0.1:8643"
    public_relay_url = ""
    relay_same_origin = False
    cache_dir = None
    web_dir = ROOTS[0]
    build_dir = ROOTS[1]
    roots = list(ROOTS)
    chat_log = []
    _chat_lock = threading.Lock()
    request_count = 0
    _count_lock = threading.Lock()
    timeout = 120

    def guess_type(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext in MIME_TYPES:
            return MIME_TYPES[ext]
        return super().guess_type(path)

    def address_string(self):
        xri = (self.headers.get("X-Real-IP") or "").strip()
        if xri:
            return xri
        xff = (self.headers.get("X-Forwarded-For") or "").strip()
        if xff:
            first = xff.split(",", 1)[0].strip()
            if first:
                return first
        return self.client_address[0]

    def _is_relay_same_origin(self):
        if self.relay_same_origin:
            return True
        return any(self.headers.get(h) for h in ("X-Relay-Proxy", "X-Forwarded-Host", "X-Forwarded-Proto"))

    @classmethod
    def get_manifest(cls):
        if cls.manifest_bytes is None and cls.install:
            cls.manifest_bytes = manifest(cls.install)
        return cls.manifest_bytes

    @classmethod
    def get_bundle(cls):
        if cls.bundle_entries is None and cls.install:
            entries = bundle_index(cls.install)
            cls.bundle_entries = entries
            cls.bundle_total = bundle_size(entries)
            max_mtime = max((mt for _, _, _, mt in entries), default=0)
            cls.bundle_etag = '"%x-%x-%x"' % (len(entries), cls.bundle_total, max_mtime)
        return cls.bundle_entries, cls.bundle_total, cls.bundle_etag

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
        xfp = (self.headers.get("X-Forwarded-Proto") or "").strip().lower()
        if xfp == "https" or self.headers.get("Tailscale-User-Login"):
            self.send_header("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; "
                         "script-src 'self' 'wasm-unsafe-eval' blob:; "
                         "style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' blob: data:; "
                         "media-src 'self' blob: data:; "
                         "connect-src 'self' ws: wss:; "
                         "worker-src 'self' blob:; "
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
        for r in self.roots:
            p = self.inside(r, rel)
            if p and os.path.exists(p):
                return p
        return self.inside(self.roots[0], rel) if self.roots else ""

    @staticmethod
    def inside(root, rel):
        rr = os.path.realpath(root)
        p = os.path.realpath(os.path.join(rr, rel))
        return p if p == rr or p.startswith(rr + os.sep) else ""

    def _json_response(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        body = self._maybe_gzip(body, "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _maybe_gzip(self, body, content_type):
        if len(body) < GZIP_MIN:
            return body
        base_ct = content_type.split(";", 1)[0].strip()
        if base_ct not in GZIP_TYPES:
            return body
        ae = self.headers.get("Accept-Encoding", "")
        if "gzip" not in ae:
            return body
        compressed = gzip.compress(body, compresslevel=6)
        if len(compressed) >= len(body):
            return body
        self.send_header("Content-Encoding", "gzip")
        return compressed

    @staticmethod
    def _parse_range(header, total):
        if not header or not header.startswith("bytes=") or total <= 0:
            return None
        spec = header[6:].split(",", 1)[0].strip()
        if "-" not in spec:
            return None
        s, e = spec.split("-", 1)
        try:
            if s == "":
                suffix = int(e)
                if suffix <= 0:
                    return False
                start = max(0, total - suffix)
                end = total - 1
            else:
                start = int(s)
                end = int(e) if e else total - 1
        except ValueError:
            return False
        if start < 0 or start >= total or end < start:
            return False
        end = min(end, total - 1)
        return (start, end)

    def _stream_file(self, fpath, offset, length):
        with open(fpath, "rb") as f:
            if offset:
                f.seek(offset)
            rem = length
            while rem > 0:
                buf = f.read(min(STREAM_CHUNK, rem))
                if not buf:
                    break
                self.wfile.write(buf)
                rem -= len(buf)

    def _send_file_with_etag(self, fpath, content_type):
        try:
            stat = os.stat(fpath)
        except OSError:
            self.send_error(404)
            return
        etag = '"%x-%x"' % (int(stat.st_mtime), stat.st_size)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("X-Raw-Length", str(stat.st_size))
            self.end_headers()
            return

        ae = self.headers.get("Accept-Encoding", "")
        base_ct = content_type.split(";", 1)[0].strip()
        want_gzip = (stat.st_size >= GZIP_MIN and base_ct in GZIP_TYPES and "gzip" in ae)

        if want_gzip and stat.st_size >= GZ_CACHE_MIN:
            gz_path = _ensure_gz(fpath, stat.st_mtime, cache_dir=self.cache_dir)
            if gz_path:
                gz_size = os.path.getsize(gz_path)
                if gz_size < stat.st_size:
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("ETag", etag)
                    self.send_header("X-Raw-Length", str(stat.st_size))
                    self.send_header("Content-Encoding", "gzip")
                    self.send_header("Vary", "Accept-Encoding")
                    self.send_header("Content-Length", str(gz_size))
                    self.end_headers()
                    if self.command != "HEAD":
                        self._stream_file(gz_path, 0, gz_size)
                    return

        if not want_gzip:
            rng = self._parse_range(self.headers.get("Range"), stat.st_size)
            if rng is False:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{stat.st_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if rng is not None:
                start, end = rng
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", content_type)
                self.send_header("ETag", etag)
                self.send_header("X-Raw-Length", str(stat.st_size))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{stat.st_size}")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                if self.command != "HEAD":
                    self._stream_file(fpath, start, length)
                return
            if stat.st_size >= GZ_CACHE_MIN:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("ETag", etag)
                self.send_header("X-Raw-Length", str(stat.st_size))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(stat.st_size))
                self.end_headers()
                if self.command != "HEAD":
                    self._stream_file(fpath, 0, stat.st_size)
                return

        with open(fpath, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("ETag", etag)
        self.send_header("X-Raw-Length", str(stat.st_size))
        self.send_header("Vary", "Accept-Encoding")
        body = self._maybe_gzip(body, content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _proxy_relay(self, parsed):
        p = parsed.path
        url = f"{self.relay_url.rstrip('/')}{self.path}"
        client_ip = self.address_string()
        req = urllib.request.Request(url, headers={
            "X-Real-IP": client_ip,
            "X-Forwarded-For": client_ip,
        })
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                raw = resp.read()
                data = json.loads(raw.decode("utf-8", "replace"))
                if p.endswith("/rooms") and "chat" not in data:
                    with self._chat_lock:
                        data["chat"] = list(Handler.chat_log)
                return self._json_response(data, status=resp.status)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                data = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                data = {"error": str(e)}
            return self._json_response(data, status=e.code)
        except Exception as e:
            if p.endswith("/chat"):
                qs = urllib.parse.parse_qs(parsed.query)
                sender = (qs.get("from", ["Player"])[0] or "Player").strip()[:15] or "Player"
                text = (qs.get("text", [""])[0] or "").strip()[:160]
                with self._chat_lock:
                    if text:
                        Handler.chat_log.append({"from": sender, "text": text, "ts": int(time.time())})
                        if len(Handler.chat_log) > 30:
                            del Handler.chat_log[:-30]
                    return self._json_response({"ok": True, "chat": list(Handler.chat_log)})
            return self._json_response({"error": str(e)}, status=502)

    def do_GET(self):
        self._req_start = time.monotonic()
        with self._count_lock:
            Handler.request_count += 1
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if path == "/health":
            v, _ = _git_version(self.build_dir)
            return self._json_response({
                "ok": True,
                "version": v,
                "install": bool(self.install),
                "requests": Handler.request_count,
            })

        if path == "/health/deep":
            relay_url = f"{self.relay_url.rstrip('/')}/health"
            try:
                with urllib.request.urlopen(relay_url, timeout=2) as resp:
                    relay_info = json.loads(resp.read().decode("utf-8", "replace"))
                    if resp.status == 200 and relay_info.get("ok"):
                        return self._json_response({"ok": True, "web": True, "relay": relay_info})
                    return self._json_response({"ok": False, "web": True, "relay": relay_info}, status=503)
            except Exception as e:
                return self._json_response({"ok": False, "web": True, "relay": {"ok": False, "error": str(e)}}, status=503)

        if path.startswith("/relay/"):
            return self._proxy_relay(parsed)

        if path == "/api/version":
            v, wasm_size = _git_version(self.build_dir)
            return self._json_response({"version": v, "wasm_size": wasm_size})

        if path == "/api/me":
            return self._json_response({
                "owner": True,
                "install": bool(self.install),
                "relay_same_origin": self._is_relay_same_origin(),
                "relay_ws_url": self.public_relay_url,
            })

        if path == "/favicon.ico":
            fav_path = self.inside(self.roots[0], "favicon.ico") if self.roots else ""
            if fav_path and os.path.isfile(fav_path):
                return self._send_file_with_etag(fav_path, MIME_TYPES[".ico"])
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(FAVICON)))
            self.send_header("Cache-Control", "public, max-age=604800")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(FAVICON)
            return

        if path == "/install/bundle.tar":
            if not self.install_allowed():
                self.send_error(404)
                return
            entries, total, etag = self.get_bundle()
            if etag and self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-tar")
            if etag:
                self.send_header("ETag", etag)
            self.send_header("Accept-Ranges", "none")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Content-Length", str(total))
            self.end_headers()
            if self.command == "HEAD":
                return
            for rel, full, size, mtime in entries:
                self.wfile.write(_tar_header(rel, size, mtime))
                rem = size
                with open(full, "rb") as f:
                    while rem > 0:
                        chunk = f.read(min(STREAM_CHUNK, rem))
                        if not chunk:
                            raise IOError(f"short read on {full}")
                        self.wfile.write(chunk)
                        rem -= len(chunk)
                pad = (-size) & 511
                if pad:
                    self.wfile.write(b"\0" * pad)
            self.wfile.write(b"\0" * 1024)
            return

        if path == "/install/manifest.json":
            if not self.install_allowed():
                self.send_error(404)
                return
            body = self.get_manifest()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            body = self._maybe_gzip(body, "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return

        # ETag and proper MIME support for static files
        fpath = self.translate_path(self.path)
        if fpath and os.path.isdir(fpath):
            fpath = os.path.join(fpath, "index.html")
        if fpath and os.path.isfile(fpath):
            ext = os.path.splitext(fpath)[1].lower()
            return self._send_file_with_etag(fpath, MIME_TYPES.get(ext, self.guess_type(fpath)))

        self.send_error(404)

    def do_HEAD(self):
        self.do_GET()

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
        except ConnectionError:
            pass


def _watch_self(server, build_dir):
    path = os.path.abspath(__file__)
    wasm_path = os.path.join(build_dir, "aoe2.wasm")
    try:
        initial = os.stat(path).st_mtime
    except OSError:
        return
    try:
        last_wasm = os.stat(wasm_path).st_mtime
    except OSError:
        last_wasm = None
    while True:
        time.sleep(2)
        try:
            if os.stat(path).st_mtime != initial:
                log.info("serve.py modified on disk, restarting...")
                threading.Thread(target=server.shutdown, daemon=True).start()
                threading.Timer(3.0, lambda: os._exit(0)).start()
                return
        except OSError:
            pass
        try:
            cur_wasm = os.stat(wasm_path).st_mtime
            if cur_wasm != last_wasm:
                last_wasm = cur_wasm
                log.info("aoe2.wasm updated on disk, refreshing gzip cache...")
                threading.Thread(target=_ensure_gz, args=(wasm_path, cur_wasm),
                                 kwargs={"cache_dir": Handler.cache_dir}, daemon=True).start()
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("AOE2_HOST") or "127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("AOE2_PORT") or 8642))
    ap.add_argument("--install", default=os.environ.get("AOE2_INSTALL_DIR") or None)
    ap.add_argument("--no-install", action="store_true", default=(os.environ.get("AOE2_NO_INSTALL") == "1"))
    ap.add_argument("--owner", default=os.environ.get("AOE2_OWNER") or None,
                    help="Tailscale login (email) of the owner")
    ap.add_argument("--relay-url", default=(os.environ.get("AOE2_RELAY_URL") or "http://127.0.0.1:8643").rstrip("/"))
    ap.add_argument("--public-relay-url", default=os.environ.get("AOE2_PUBLIC_RELAY_URL") or "")
    ap.add_argument("--relay-same-origin", action="store_true",
                    default=(os.environ.get("AOE2_RELAY_SAME_ORIGIN") == "1"))
    ap.add_argument("--cache-dir", default=os.environ.get("AOE2_CACHE_DIR") or None)
    ap.add_argument("--web-dir", default=os.environ.get("AOE2_WEB_DIR") or os.path.join(REPO, "web"))
    ap.add_argument("--build-dir", default=os.environ.get("AOE2_BUILD_DIR") or os.path.join(REPO, "build", "web"))
    ap.add_argument("--no-watch", action="store_true", default=(os.environ.get("AOE2_NO_WATCH") == "1"))
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stderr)

    install = None if a.no_install else a.install
    if not a.no_install and not install:
        for cand in (os.path.join(os.path.dirname(REPO), "aoe2-recomp-work", "install"),
                     "/root/aoe2-recomp-work/install",
                     os.path.expanduser("~/aoe2-recomp-work/install"),
                     os.path.expanduser("~/aoe2-install")):
            if os.path.isdir(os.path.join(cand, "age2_x1")):
                install = cand
                break
    Handler.web_dir = os.path.realpath(a.web_dir)
    Handler.build_dir = os.path.realpath(a.build_dir)
    Handler.roots = [Handler.web_dir, Handler.build_dir]
    Handler.relay_url = a.relay_url.rstrip("/")
    Handler.public_relay_url = a.public_relay_url.strip()
    Handler.relay_same_origin = bool(a.relay_same_origin)
    Handler.cache_dir = os.path.realpath(a.cache_dir) if a.cache_dir else None
    Handler.install = os.path.realpath(install) if install else None
    Handler.owner = a.owner.lower() if a.owner else None
    if Handler.install and os.path.isdir(Handler.install):
        Handler.manifest_bytes = None
        Handler.bundle_entries = None
        Handler.bundle_total = 0
        Handler.bundle_etag = None
        Handler.get_manifest()
        Handler.get_bundle()
    else:
        Handler.install = None
        Handler.manifest_bytes = None
        Handler.bundle_entries = None
        Handler.bundle_total = 0
        Handler.bundle_etag = None

    wasm_path = os.path.join(Handler.build_dir, "aoe2.wasm")
    if os.path.isfile(wasm_path):
        threading.Thread(target=_ensure_gz, args=(wasm_path, os.stat(wasm_path).st_mtime),
                         kwargs={"cache_dir": Handler.cache_dir}, daemon=True).start()

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    server = socketserver.ThreadingTCPServer((a.host, a.port), Handler)
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
    if not a.no_watch:
        threading.Thread(target=_watch_self, args=(server, Handler.build_dir), daemon=True).start()

    info = f"http://{a.host}:{a.port}/"
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
