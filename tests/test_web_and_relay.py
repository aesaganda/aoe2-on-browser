"""Unit and integration tests for tools/web/serve.py and tools/net/relay.py."""

import asyncio
import errno
import gzip
import http.client
import http.server
import io
import json
import os
import socket
import socketserver
import struct
import sys
import tarfile
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

import websockets

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools", "web"))
sys.path.insert(0, os.path.join(ROOT, "tools", "net"))

import relay  # noqa: E402
import serve  # noqa: E402


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _unused_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_handler_class(**attrs):
    class QuietTestHandler(serve.Handler):
        def log_message(self, format, *args):  # noqa: A002
            pass

    for key, val in attrs.items():
        setattr(QuietTestHandler, key, val)
    return QuietTestHandler


def _http_get(host, port, path, headers=None, method="GET"):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, dict(resp.getheaders()), body
    finally:
        conn.close()


class TestServe(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = self._tmp.name
        self.web_dir = os.path.join(self.tmp_dir, "web")
        self.build_dir = os.path.join(self.tmp_dir, "build")
        self.install_dir = os.path.join(self.tmp_dir, "install")
        self.cache_dir = os.path.join(self.tmp_dir, "cache")
        for d in (self.web_dir, self.build_dir, self.install_dir, self.cache_dir):
            os.makedirs(d, exist_ok=True)

        with open(os.path.join(self.web_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<!doctype html><html><body>ok</body></html>")
        with open(os.path.join(self.web_dir, "app.js"), "w", encoding="utf-8") as f:
            f.write("console.log('aoe2');\n")
        with open(os.path.join(self.web_dir, "manifest.json"), "w", encoding="utf-8") as f:
            f.write('{"name":"aoe2"}')
        with open(os.path.join(self.web_dir, "range.bin"), "wb") as f:
            f.write(b"0123456789abcdef")

        subdir = os.path.join(self.web_dir, "secret_dir")
        os.makedirs(subdir, exist_ok=True)
        with open(os.path.join(subdir, "secret.txt"), "w", encoding="utf-8") as f:
            f.write("hidden")

        self.wasm_bytes = b"\x00asm\x01\x00\x00\x00" + b"X" * 120
        with open(os.path.join(self.build_dir, "aoe2.wasm"), "wb") as f:
            f.write(self.wasm_bytes)

        os.makedirs(os.path.join(self.install_dir, "Data"), exist_ok=True)
        with open(os.path.join(self.install_dir, "Data", "empires.dat"), "wb") as f:
            f.write(b"EMPIRES-DAT-TEST-PAYLOAD")
        with open(os.path.join(self.install_dir, "readme.txt"), "wb") as f:
            f.write(b"hello install")

        self._servers = []

    def tearDown(self):
        for srv in reversed(self._servers):
            srv.shutdown()
            srv.server_close()
        self._tmp.cleanup()

    def _start_serve(self, **handler_overrides):
        defaults = {
            "install": self.install_dir,
            "owner": None,
            "manifest_bytes": None,
            "bundle_entries": None,
            "bundle_total": 0,
            "bundle_etag": None,
            "relay_url": f"http://127.0.0.1:{_unused_port()}",
            "public_relay_url": "wss://relay.example.test/ws",
            "relay_same_origin": False,
            "cache_dir": self.cache_dir,
            "web_dir": self.web_dir,
            "build_dir": self.build_dir,
            "roots": [self.web_dir, self.build_dir],
            "chat_log": [],
        }
        defaults.update(handler_overrides)
        cls = _make_handler_class(**defaults)
        srv = _ThreadingHTTPServer(("127.0.0.1", 0), cls)
        self._servers.append(srv)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv.server_address

    def _start_mock_relay(self):
        recorded_requests = []
        state = {"chat_calls": 0}

        class MockRelayHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                pass

            def do_GET(self):
                recorded_requests.append({
                    "path": self.path,
                    "x_real_ip": self.headers.get("X-Real-IP"),
                    "x_forwarded_for": self.headers.get("X-Forwarded-For"),
                })
                p = urllib.parse.urlsplit(self.path).path
                if p.endswith("/health"):
                    body = json.dumps({"ok": True, "rooms": 1, "connections": 2}).encode()
                    self.send_response(200)
                elif p.endswith("/chat"):
                    state["chat_calls"] += 1
                    if state["chat_calls"] >= 2:
                        body = json.dumps({"ok": False, "error": "rate_limited", "chat": []}).encode()
                        self.send_response(429)
                    else:
                        body = json.dumps({"ok": True, "chat": [{"from": "U", "text": "hi", "ts": 1}]}).encode()
                        self.send_response(200)
                else:
                    body = json.dumps({"rooms": [], "chat": []}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = _ThreadingHTTPServer(("127.0.0.1", 0), MockRelayHandler)
        self._servers.append(srv)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv.server_address, recorded_requests

    def test_http11_keep_alive(self):
        host, port = self._start_serve()
        conn = http.client.HTTPConnection(host, port, timeout=5)
        try:
            conn.request("GET", "/health")
            r1 = conn.getresponse()
            b1 = r1.read()
            self.assertEqual(r1.status, 200)
            self.assertEqual(r1.version, 11)
            self.assertFalse(r1.will_close)
            sock1 = conn.sock.getsockname()
            self.assertTrue(json.loads(b1)["ok"])

            conn.request("GET", "/api/version")
            r2 = conn.getresponse()
            b2 = r2.read()
            self.assertEqual(r2.status, 200)
            self.assertEqual(r2.version, 11)
            self.assertFalse(r2.will_close)
            self.assertEqual(conn.sock.getsockname(), sock1)
            self.assertEqual(json.loads(b2)["wasm_size"], len(self.wasm_bytes))
        finally:
            conn.close()

    def test_mime_types_and_security_headers_and_hsts(self):
        host, port = self._start_serve()
        cases = [
            ("/index.html", "text/html"),
            ("/app.js", "text/javascript"),
            ("/aoe2.wasm", "application/wasm"),
            ("/manifest.json", "application/json"),
        ]
        for path, expected_ct in cases:
            status, hdrs, _ = _http_get(host, port, path)
            lower = {k.lower(): v for k, v in hdrs.items()}
            self.assertEqual(status, 200, path)
            self.assertTrue(lower["content-type"].startswith(expected_ct), (path, lower["content-type"]))
            self.assertEqual(lower["cross-origin-opener-policy"], "same-origin")
            self.assertEqual(lower["cross-origin-embedder-policy"], "require-corp")
            self.assertEqual(lower["cross-origin-resource-policy"], "same-origin")
            self.assertEqual(lower["x-content-type-options"], "nosniff")
            self.assertEqual(lower["x-frame-options"], "DENY")
            self.assertIn("connect-src 'self' ws: wss:", lower["content-security-policy"])
            self.assertIn("'wasm-unsafe-eval'", lower["content-security-policy"])
            self.assertNotIn("strict-transport-security", lower)

        status, hdrs, _ = _http_get(host, port, "/health", headers={"X-Forwarded-Proto": "https"})
        lower = {k.lower(): v for k, v in hdrs.items()}
        self.assertEqual(status, 200)
        self.assertEqual(lower["strict-transport-security"], "max-age=63072000; includeSubDomains")

    def test_health_and_health_deep(self):
        (rhost, rport), _ = self._start_mock_relay()
        host, port = self._start_serve(relay_url=f"http://{rhost}:{rport}")

        st, _, body = _http_get(host, port, "/health")
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["ok"])

        st, _, body = _http_get(host, port, "/health/deep")
        self.assertEqual(st, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertTrue(data["web"])
        self.assertTrue(data["relay"]["ok"])

        dead_port = _unused_port()
        host2, port2 = self._start_serve(relay_url=f"http://127.0.0.1:{dead_port}")
        st2, _, body2 = _http_get(host2, port2, "/health/deep")
        self.assertEqual(st2, 503)
        data2 = json.loads(body2)
        self.assertFalse(data2["ok"])
        self.assertTrue(data2["web"])
        self.assertFalse(data2["relay"]["ok"])

    def test_api_version_and_api_me(self):
        host, port = self._start_serve(public_relay_url="wss://relay.example.test", relay_same_origin=False)
        with mock.patch.dict(os.environ, {"AOE2_VERSION": "v2026-test-sha"}):
            st, _, body = _http_get(host, port, "/api/version")
        self.assertEqual(st, 200)
        ver = json.loads(body)
        expected_mtime = f"{int(os.stat(os.path.join(self.build_dir, 'aoe2.wasm')).st_mtime):x}"
        self.assertEqual(ver["version"], f"v2026-test-sha-{expected_mtime}")
        self.assertEqual(ver["wasm_size"], len(self.wasm_bytes))

        st, _, body = _http_get(host, port, "/api/me")
        self.assertEqual(st, 200)
        me = json.loads(body)
        self.assertFalse(me["relay_same_origin"])
        self.assertTrue(me["install"])
        self.assertEqual(me["relay_ws_url"], "wss://relay.example.test")

        st, _, body = _http_get(host, port, "/api/me", headers={"X-Relay-Proxy": "1"})
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["relay_same_origin"])

        host2, port2 = self._start_serve(install=None, relay_same_origin=True)
        st, _, body = _http_get(host2, port2, "/api/me")
        me2 = json.loads(body)
        self.assertFalse(me2["install"])
        self.assertTrue(me2["relay_same_origin"])

    def test_install_bundle_tar_etag_304_and_extraction(self):
        host, port = self._start_serve()
        st, hdrs, body = _http_get(host, port, "/install/bundle.tar")
        lower = {k.lower(): v for k, v in hdrs.items()}
        self.assertEqual(st, 200)
        self.assertEqual(lower["content-type"], "application/x-tar")
        self.assertEqual(lower["accept-ranges"], "none")
        self.assertEqual(lower["x-accel-buffering"], "no")
        self.assertIn("etag", lower)
        etag = lower["etag"]

        with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as tf:
            members = {m.name: tf.extractfile(m).read() for m in tf.getmembers() if m.isfile()}
        self.assertEqual(members["Data/empires.dat"], b"EMPIRES-DAT-TEST-PAYLOAD")
        self.assertEqual(members["readme.txt"], b"hello install")

        st304, _, body304 = _http_get(host, port, "/install/bundle.tar", headers={"If-None-Match": etag})
        self.assertEqual(st304, 304)
        self.assertEqual(body304, b"")

        st_head, hdrs_head, body_head = _http_get(host, port, "/install/bundle.tar", method="HEAD")
        self.assertEqual(st_head, 200)
        self.assertEqual(body_head, b"")
        self.assertEqual(dict((k.lower(), v) for k, v in hdrs_head.items())["etag"], etag)

        host_no_inst, port_no_inst = self._start_serve(install=None)
        st404, _, _ = _http_get(host_no_inst, port_no_inst, "/install/bundle.tar")
        self.assertEqual(st404, 404)

    def test_range_206_and_416(self):
        host, port = self._start_serve()
        st206, hdrs206, body206 = _http_get(
            host, port, "/range.bin", headers={"Accept-Encoding": "identity", "Range": "bytes=2-6"}
        )
        self.assertEqual(st206, 206)
        self.assertEqual(body206, b"23456")
        self.assertEqual(dict((k.lower(), v) for k, v in hdrs206.items())["content-range"], "bytes 2-6/16")

        st416, hdrs416, body416 = _http_get(
            host, port, "/range.bin", headers={"Accept-Encoding": "identity", "Range": "bytes=999-"}
        )
        self.assertEqual(st416, 416)
        self.assertEqual(body416, b"")
        self.assertEqual(dict((k.lower(), v) for k, v in hdrs416.items())["content-length"], "0")

    def test_directory_listing_blocked_404(self):
        host, port = self._start_serve()
        for path in ("/secret_dir", "/secret_dir/"):
            st, _, _ = _http_get(host, port, path)
            self.assertEqual(st, 404, path)

    def test_relay_proxy_preserves_429_and_forwards_client_ip(self):
        (rhost, rport), recorded = self._start_mock_relay()
        host, port = self._start_serve(relay_url=f"http://{rhost}:{rport}")

        st1, _, b1 = _http_get(
            host,
            port,
            "/relay/chat?from=Tester&text=hello",
            headers={"X-Forwarded-For": "203.0.113.42, 10.0.0.1"},
        )
        self.assertEqual(st1, 200)
        self.assertTrue(json.loads(b1)["ok"])

        st2, _, b2 = _http_get(
            host,
            port,
            "/relay/chat?from=Tester&text=spam",
            headers={"X-Real-IP": "203.0.113.42"},
        )
        self.assertEqual(st2, 429)
        self.assertEqual(json.loads(b2)["error"], "rate_limited")

        self.assertEqual(recorded[0]["x_real_ip"], "203.0.113.42")
        self.assertEqual(recorded[0]["x_forwarded_for"], "203.0.113.42")
        self.assertEqual(recorded[1]["x_real_ip"], "203.0.113.42")

    def test_ensure_gz_readonly_fallback(self):
        ro_dir = os.path.join(self.tmp_dir, "ro_src")
        ro_cache = os.path.join(self.tmp_dir, "ro_cache")
        os.makedirs(ro_dir, exist_ok=True)
        os.makedirs(ro_cache, exist_ok=True)
        src = os.path.join(ro_dir, "asset.js")
        raw = b"var x = 'compressible payload';\n" * 64
        with open(src, "wb") as f:
            f.write(raw)
        mtime = os.path.getmtime(src)

        os.chmod(ro_dir, 0o555)
        try:
            if os.geteuid() != 0:
                gz_path = serve._ensure_gz(src, mtime, cache_dir=ro_cache)
                self.assertIsNotNone(gz_path)
                self.assertTrue(gz_path.startswith(ro_cache))
                with gzip.open(gz_path, "rb") as gz:
                    self.assertEqual(gz.read(), raw)
            else:
                # Root bypasses 0o555 DAC checks; exercise EROFS fallback on ro_dir while using real gzip.open for ro_cache.
                real_gzip_open = gzip.open

                def fake_gzip_open(filename, *args, **kwargs):
                    if os.path.dirname(os.path.abspath(filename)) == os.path.abspath(ro_dir):
                        raise OSError(errno.EROFS, "Read-only file system")
                    return real_gzip_open(filename, *args, **kwargs)

                with mock.patch.object(serve.gzip, "open", side_effect=fake_gzip_open):
                    gz_path = serve._ensure_gz(src, mtime, cache_dir=ro_cache)
                self.assertIsNotNone(gz_path)
                self.assertTrue(gz_path.startswith(ro_cache))
                with gzip.open(gz_path, "rb") as gz:
                    self.assertEqual(gz.read(), raw)
        finally:
            os.chmod(ro_dir, 0o755)


class TestRelay(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.relay = relay.Relay(max_rooms=10)
        self.relay.start_cleanup()
        ws_handler, _, process_request = relay.create_relay_handlers(
            self.relay, max_conns=20, max_conns_per_ip=5
        )
        self.ws_handler = ws_handler
        self.process_request = process_request
        self.server = await websockets.serve(
            ws_handler,
            "127.0.0.1",
            0,
            process_request=process_request,
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        cleanup_task = getattr(self.relay, "_cleanup_task", None)
        if cleanup_task is not None:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass

    async def test_http_endpoints_and_chat_rate_limit_per_ip(self):
        st, _, b = await asyncio.to_thread(_http_get, "127.0.0.1", self.port, "/health")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(b), {"ok": True, "rooms": 0, "connections": 0})

        st, _, b = await asyncio.to_thread(_http_get, "127.0.0.1", self.port, "/stats")
        self.assertEqual(st, 200)
        stats = json.loads(b)
        self.assertEqual(stats["max_rooms"], 10)
        self.assertEqual(stats["max_conns"], 20)
        self.assertEqual(stats["rooms"], [])

        st, _, b = await asyncio.to_thread(_http_get, "127.0.0.1", self.port, "/rooms")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(b), {"rooms": [], "chat": [], "online": 0})

        ip1_headers = {"X-Forwarded-For": "198.51.100.1"}
        for i in range(relay.CHAT_RATE_LIMIT):
            st, _, b = await asyncio.to_thread(
                _http_get, "127.0.0.1", self.port, f"/chat?from=Alice&text=msg{i}", ip1_headers
            )
            self.assertEqual(st, 200)
            self.assertTrue(json.loads(b)["ok"])

        st, _, b = await asyncio.to_thread(
            _http_get, "127.0.0.1", self.port, "/chat?from=Alice&text=overflow", ip1_headers
        )
        self.assertEqual(st, 429)
        self.assertEqual(json.loads(b)["error"], "rate_limited")

        ip2_headers = {"X-Forwarded-For": "198.51.100.2"}
        st, _, b = await asyncio.to_thread(
            _http_get, "127.0.0.1", self.port, "/chat?from=Bob&text=allowed", ip2_headers
        )
        self.assertEqual(st, 200)
        data = json.loads(b)
        self.assertTrue(data["ok"])
        self.assertEqual(data["chat"][-1]["from"], "Bob")

    async def test_process_request_dual_api_compatibility(self):
        import types
        import websockets.http11

        res_legacy = await self.process_request("/health", {"Host": "localhost"})
        self.assertIsInstance(res_legacy, tuple)
        status_legacy, hdrs_legacy, body_legacy = res_legacy
        self.assertEqual(int(status_legacy), 200)
        self.assertTrue(json.loads(body_legacy)["ok"])
        self.assertTrue(any(k.lower() == "content-type" for k, _ in hdrs_legacy))

        fake_conn = types.SimpleNamespace(remote_address=("127.0.0.1", 55555))
        fake_req = types.SimpleNamespace(path="/rooms", headers={"X-Real-IP": "203.0.113.99"})
        res_v14 = await self.process_request(fake_conn, fake_req)

        self.assertIsInstance(res_v14, websockets.http11.Response)
        self.assertEqual(res_v14.status_code, 200)
        self.assertEqual(json.loads(res_v14.body), {"rooms": [], "chat": [], "online": 0})
        self.assertEqual(res_v14.headers["Content-Type"], "application/json; charset=utf-8")

    async def test_ws_directplay_hello_list_create_roundtrip(self):
        url = f"ws://127.0.0.1:{self.port}/relay"
        app_guid = bytes(range(16))
        desc = bytearray(80)
        struct.pack_into("<II", desc, 0, 80, 0)
        desc[24:40] = app_guid
        struct.pack_into("<I", desc, 40, 4)

        async with websockets.connect(url) as ws:
            await ws.send(struct.pack("<BI", relay.HELLO, 1) + relay.s(b"Commander"))
            t, pad, req, hr = struct.unpack("<BIII", await ws.recv())
            self.assertEqual((t, pad, req, hr), (relay.REPLY, 0, 1, relay.DP_OK))

            await ws.send(struct.pack("<BI", relay.LIST, 2) + app_guid)
            frame = await ws.recv()
            t, pad, req, hr, count = struct.unpack_from("<BIIII", frame, 0)
            self.assertEqual((t, pad, req, hr, count), (relay.REPLY, 0, 2, relay.DP_OK, 0))

            await ws.send(
                struct.pack("<BI", relay.CREATE, 3) + bytes(desc) + relay.s(b"Arena") + relay.s(b"")
            )
            cframe = await ws.recv()
            t, pad, req, hr = struct.unpack_from("<BIII", cframe, 0)
            self.assertEqual((t, pad, req, hr), (relay.REPLY, 0, 3, relay.DP_OK))

            await ws.send(struct.pack("<BI", relay.LIST, 4) + app_guid)
            lframe = await ws.recv()
            t, pad, req, hr, count = struct.unpack_from("<BIIII", lframe, 0)
            self.assertEqual((t, pad, req, hr, count), (relay.REPLY, 0, 4, relay.DP_OK, 1))
            self.assertIn(b"Arena", lframe)

            st, _, b = await asyncio.to_thread(_http_get, "127.0.0.1", self.port, "/rooms")
            self.assertEqual(st, 200)
            payload = json.loads(b)
            self.assertGreaterEqual(payload["online"], 1)
            rooms = payload["rooms"]
            self.assertEqual(len(rooms), 1)
            self.assertEqual(rooms[0]["name"], "Arena")
            self.assertEqual(rooms[0]["host"], "Commander")
            self.assertIsInstance(rooms[0]["age_s"], int)
            self.assertGreaterEqual(rooms[0]["age_s"], 0)
            self.assertEqual(rooms[0]["max"], 4)
            self.assertTrue(rooms[0]["open"])
            self.assertFalse(rooms[0]["password"])

    async def test_disconnect_during_broadcast_handles_connection_closed(self):
        r = relay.Relay(max_rooms=5)
        closed_err = websockets.exceptions.ConnectionClosedOK(None, None)

        peer_closing = False

        async def ok_send(_frame):
            return None

        async def peer_send(_frame):
            if peer_closing:
                raise closed_err
            return None

        host_conn = relay.Conn(ok_send, "127.0.0.1")
        peer_conn = relay.Conn(peer_send, "127.0.0.1")

        app_guid = bytes(range(16))
        desc = bytearray(80)
        struct.pack_into("<II", desc, 0, 80, 0)
        desc[24:40] = app_guid
        struct.pack_into("<I", desc, 40, 4)

        await r.handle(host_conn, struct.pack("<BI", relay.CREATE, 1) + bytes(desc) + relay.s(b"DropRoom") + relay.s(b""))
        room = next(iter(r.rooms.values()))

        await r.handle(peer_conn, struct.pack("<BI", relay.JOIN, 2) + room.instance + relay.s(b""))
        await r.handle(
            host_conn,
            struct.pack("<BII", relay.NEWPLAYER, 3, 0) + relay.s(b"Host") + relay.s(b"Host") + relay.blob(b""),
        )
        await r.handle(
            peer_conn,
            struct.pack("<BII", relay.NEWPLAYER, 4, 0) + relay.s(b"Peer") + relay.s(b"Peer") + relay.blob(b""),
        )
        host_player = next(p for p in room.players.values() if p.conn is host_conn)
        peer_player = next(p for p in room.players.values() if p.conn is peer_conn)

        # Simulate peer WebSocket closing concurrently while host sends and leaves.
        peer_closing = True

        # DirectPlay SEND to a closed WebSocket peer must not raise ConnectionClosedOK.
        await r.handle(
            host_conn,
            struct.pack("<BIIII", relay.SEND, 5, host_player.id, peer_player.id, 0) + relay.blob(b"ping"),
        )

        # to_room broadcast (host leaving triggers SYS_DELPLAYER + SYS_HOST to closed peer_conn) must not raise.
        await r.leave(host_conn)
        self.assertIs(room.host, peer_conn)
        self.assertNotIn(host_player.id, room.players)

        # Last peer leaving closes the room cleanly.
        await r.leave(peer_conn)
        self.assertEqual(len(r.rooms), 0)


if __name__ == "__main__":
    unittest.main()
