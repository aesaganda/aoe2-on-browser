#!/usr/bin/env python3
"""The lobby and relay for multiplayer (Phase 3, ADR-0006).

    tools/net/relay.py [--ws-port 8643] [--tcp-port 8644] [--host 127.0.0.1]

Players' games talk DirectPlay to src/net/dplay_net.c, which carries it here: the
browser build over a WebSocket (ws://host:8643/relay), the native build over TCP
(port 8644, each frame preceded by its u32 length). This server keeps the rooms
(DirectPlay sessions) and their players, and forwards every message. Nothing of
the game passes through it but the players' own DirectPlay traffic.

Frames (little-endian): u8 type, u32 request id, then the body. str = u16 length +
bytes, blob = u32 length + bytes, desc = a DPSESSIONDESC2 (80 bytes; its string
pointers are ignored) followed by str name, str password.

  client -> server                           server -> client
  1  HELLO     str name                      100 REPLY    u32 req, u32 hresult, body
  2  LIST      guid application              101 MSG      u32 from, u32 to, blob
  3  CREATE    desc                          102 SYS_NEWPLAYER   player, u32 count
  4  JOIN      guid instance, str password   103 SYS_DELPLAYER   player, u32 count
  5  NEWPLAYER u32 flags, str, str, blob     104 SYS_SETDESC     desc
  6  DELPLAYER u32 id                        105 SYS_SETPDATA    u32 id, blob
  7  SEND      u32 from, u32 to, u32 flags,  106 SYS_SETPNAME    u32 id, str, str
               blob                          107 SYS_HOST        (you are the host now)
  8  SETDESC   desc                          108 SYS_LOST        (the session is gone)
  9  SETPDATA  u32 id, blob
  10 SETPNAME  u32 id, str short, str long   player = u32 id, u32 flags, str short,
  11 LEAVE                                            str long, blob data

REPLY bodies: LIST -> u32 n, n x desc. CREATE/JOIN -> desc, u32 n, n x player.
NEWPLAYER -> u32 id. Every other request -> empty.

HTTP GET .../rooms on the WebSocket port answers the rooms as JSON (id = the
guidInstance in hex, name, players, max, open, password), for the web page's game
list. GET .../health answers {"ok": true} for monitoring.
"""
import argparse
import asyncio
import http
import itertools
import json
import logging
import os
import signal
import struct
import time

import websockets

log = logging.getLogger("relay")

DP_OK = 0
DPERR_NOSESSIONS = 0x887700DC
DPERR_NONEWPLAYERS = 0x8877014A
DPERR_INVALIDPASSWORD = 0x88770154
DPERR_INVALIDPLAYER = 0x88770096
DPERR_GENERIC = 0x80004005

HELLO, LIST, CREATE, JOIN, NEWPLAYER, DELPLAYER, SEND, SETDESC, SETPDATA, SETPNAME, LEAVE = range(1, 12)
REPLY, MSG, SYS_NEWPLAYER, SYS_DELPLAYER, SYS_SETDESC, SYS_SETPDATA, SYS_SETPNAME, SYS_HOST, SYS_LOST = range(100, 109)

DPSESSION_NEWPLAYERSDISABLED = 0x01
DPSESSION_JOINDISABLED = 0x20
DPSESSION_PASSWORDREQUIRED = 0x400

MAX_FRAME = 1 << 20          # 1 MiB — no legitimate DirectPlay message is larger
MAX_CONNS = 200               # total simultaneous connections
MAX_PLAYERS_PER_ROOM = 8
MAX_ROOMS = 50
ROOM_TTL = 7200               # seconds: empty rooms are cleaned up after this


class Reader:
    __slots__ = ("b", "o")

    def __init__(self, b):
        self.b, self.o = b, 0

    def remaining(self):
        return len(self.b) - self.o

    def u8(self):
        if self.remaining() < 1:
            raise ValueError("short frame")
        v = self.b[self.o]; self.o += 1; return v

    def u32(self):
        if self.remaining() < 4:
            raise ValueError("short frame")
        v, = struct.unpack_from("<I", self.b, self.o); self.o += 4; return v

    def raw(self, n):
        if self.remaining() < n:
            raise ValueError("short frame")
        v = self.b[self.o:self.o + n]; self.o += n; return bytes(v)

    def str(self):
        if self.remaining() < 2:
            raise ValueError("short frame")
        n, = struct.unpack_from("<H", self.b, self.o); self.o += 2
        if n > 0xFFFF or self.remaining() < n:
            raise ValueError("bad string length")
        return self.raw(n)

    def blob(self):
        return self.raw(self.u32())


def s(v):
    return struct.pack("<H", len(v)) + v


def blob(v):
    return struct.pack("<I", len(v)) + v


class Player:
    __slots__ = ("id", "conn", "flags", "short", "long", "data")

    def __init__(self, pid, conn, flags, short, long_, data):
        self.id, self.conn, self.flags, self.short, self.long, self.data = pid, conn, flags, short, long_, data

    def pack(self):
        return struct.pack("<II", self.id, self.flags) + s(self.short) + s(self.long) + blob(self.data)


class Room:
    __slots__ = ("desc", "name", "password", "host", "players", "conns", "created", "last_active")

    def __init__(self, desc, name, password, host):
        self.desc = bytearray(desc)
        self.name, self.password, self.host = name, password, host
        self.players = {}
        self.conns = {host}
        now = time.monotonic()
        self.created = now
        self.last_active = now

    @property
    def instance(self):
        return bytes(self.desc[8:24])

    def max_players(self):
        return struct.unpack_from("<I", self.desc, 40)[0]

    def touch(self):
        self.last_active = time.monotonic()

    def pack(self):
        struct.pack_into("<I", self.desc, 44, len(self.players))
        flags = struct.unpack_from("<I", self.desc, 4)[0]
        if self.password:
            flags |= DPSESSION_PASSWORDREQUIRED
        struct.pack_into("<I", self.desc, 4, flags)
        return bytes(self.desc) + s(self.name) + s(b"")


class Conn:
    ids = itertools.count(1)

    def __init__(self, send, peer):
        self.send, self.peer, self.n = send, peer, next(Conn.ids)
        self.name, self.room = b"?", None


class Relay:
    def __init__(self):
        self.rooms = {}
        self.pids = itertools.count(0x10000 + (int.from_bytes(os.urandom(2), "little") << 8))
        self.conn_count = 0
        self._cleanup_task = None

    def start_cleanup(self):
        self._cleanup_task = asyncio.ensure_future(self._cleanup_loop())

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            stale = [k for k, r in self.rooms.items()
                     if not r.players and now - r.last_active > ROOM_TTL]
            for k in stale:
                r = self.rooms.pop(k, None)
                if r:
                    log.info("cleaned up stale room %r (empty for %ds)", r.name, int(now - r.last_active))

    async def reply(self, c, req, hr, body=b""):
        await c.send(struct.pack("<BIII", REPLY, 0, req, hr) + body)

    async def to_room(self, room, frame, but=None):
        for c in list(room.conns):
            if c is not but:
                try:
                    await c.send(frame)
                except (ConnectionError, asyncio.CancelledError):
                    pass

    async def handle(self, c, frame):
        if len(frame) < 5:
            return
        r = Reader(frame)
        t, req = r.u8(), r.u32()
        if t == HELLO:
            c.name = r.str()
            log.info("#%d %s: hello %r", c.n, c.peer, c.name)
            return await self.reply(c, req, DP_OK)
        if t == LIST:
            app = r.raw(16)
            rooms = [x for x in self.rooms.values() if x.desc[24:40] == app or app == bytes(16)]
            return await self.reply(c, req, DP_OK, struct.pack("<I", len(rooms)) + b"".join(x.pack() for x in rooms))
        if t == CREATE:
            if len(self.rooms) >= MAX_ROOMS:
                return await self.reply(c, req, DPERR_GENERIC)
            await self.leave(c)
            desc, name, password = bytearray(r.raw(80)), r.str(), r.str()
            desc[8:24] = os.urandom(16)
            room = Room(desc, name, password, c)
            self.rooms[room.instance] = room
            c.room = room
            log.info("#%d created room %r (max %d, application %s)", c.n, name, room.max_players(), bytes(desc[24:40]).hex())
            return await self.reply(c, req, DP_OK, room.pack() + struct.pack("<I", 0))
        if t == JOIN:
            inst, password = r.raw(16), r.str()
            room = self.rooms.get(inst)
            if not room:
                return await self.reply(c, req, DPERR_NOSESSIONS)
            if room.password and password != room.password:
                return await self.reply(c, req, DPERR_INVALIDPASSWORD)
            flags = struct.unpack_from("<I", room.desc, 4)[0]
            if flags & (DPSESSION_JOINDISABLED | DPSESSION_NEWPLAYERSDISABLED) or len(room.players) >= room.max_players() > 0:
                return await self.reply(c, req, DPERR_NONEWPLAYERS)
            await self.leave(c)
            room.conns.add(c)
            room.touch()
            c.room = room
            log.info("#%d joined room %r", c.n, room.name)
            players = list(room.players.values())
            return await self.reply(c, req, DP_OK, room.pack() + struct.pack("<I", len(players)) + b"".join(p.pack() for p in players))
        room = c.room
        if t == LEAVE:
            await self.leave(c)
            return await self.reply(c, req, DP_OK)
        if not room:
            return await self.reply(c, req, DPERR_GENERIC)
        room.touch()
        if t == NEWPLAYER:
            if len(room.players) >= MAX_PLAYERS_PER_ROOM:
                return await self.reply(c, req, DPERR_NONEWPLAYERS)
            flags, short, long_, data = r.u32(), r.str(), r.str(), r.blob()
            p = Player(next(self.pids), c, flags, short, long_, data)
            room.players[p.id] = p
            log.info("#%d player %08X %r in %r", c.n, p.id, short, room.name)
            await self.to_room(room, struct.pack("<BI", SYS_NEWPLAYER, 0) + p.pack() + struct.pack("<I", len(room.players)), but=c)
            return await self.reply(c, req, DP_OK, struct.pack("<I", p.id))
        if t == DELPLAYER:
            pid = r.u32()
            p = room.players.get(pid)
            if not p or p.conn is not c:
                return await self.reply(c, req, DPERR_INVALIDPLAYER)
            await self.drop_player(room, p)
            return await self.reply(c, req, DP_OK)
        if t == SEND:
            src, dst, flags, data = r.u32(), r.u32(), r.u32(), r.blob()
            if dst == 0:
                targets = [p for p in room.players.values() if p.id != src]
            else:
                targets = [room.players[dst]] if dst in room.players else []
            for p in targets:
                try:
                    await p.conn.send(struct.pack("<BIII", MSG, 0, src, p.id) + blob(data))
                except (ConnectionError, asyncio.CancelledError):
                    pass
            return
        if t == SETDESC:
            if c is not room.host:
                return await self.reply(c, req, DPERR_GENERIC)
            desc, name, password = r.raw(80), r.str(), r.str()
            inst = room.instance
            room.desc[:] = desc
            room.desc[8:24] = inst
            room.name, room.password = name, password
            await self.to_room(room, struct.pack("<BI", SYS_SETDESC, 0) + room.pack(), but=c)
            return await self.reply(c, req, DP_OK)
        if t == SETPDATA:
            pid, data = r.u32(), r.blob()
            p = room.players.get(pid)
            if p:
                p.data = data
                await self.to_room(room, struct.pack("<BII", SYS_SETPDATA, 0, pid) + blob(data), but=c)
            return await self.reply(c, req, DP_OK if p else DPERR_INVALIDPLAYER)
        if t == SETPNAME:
            pid, short, long_ = r.u32(), r.str(), r.str()
            p = room.players.get(pid)
            if p:
                p.short, p.long = short, long_
                await self.to_room(room, struct.pack("<BII", SYS_SETPNAME, 0, pid) + s(short) + s(long_), but=c)
            return await self.reply(c, req, DP_OK if p else DPERR_INVALIDPLAYER)
        log.warning("#%d: unknown frame type %d", c.n, t)

    async def drop_player(self, room, p):
        room.players.pop(p.id, None)
        await self.to_room(room, struct.pack("<BI", SYS_DELPLAYER, 0) + p.pack() + struct.pack("<I", len(room.players)), but=p.conn)

    async def leave(self, c):
        room, c.room = c.room, None
        if not room:
            return
        room.conns.discard(c)
        for p in [p for p in room.players.values() if p.conn is c]:
            await self.drop_player(room, p)
        if room.host is c:
            if room.conns:
                room.host = next(iter(room.conns))
                await room.host.send(struct.pack("<BI", SYS_HOST, 0))
            else:
                self.rooms.pop(room.instance, None)
                log.info("room %r closed", room.name)

    async def serve(self, c, frames):
        self.conn_count += 1
        log.info("#%d %s: connected (%d total)", c.n, c.peer, self.conn_count)
        try:
            async for f in frames:
                if len(f) > MAX_FRAME:
                    log.warning("#%d: frame too large (%d bytes), dropping", c.n, len(f))
                    continue
                try:
                    await self.handle(c, f)
                except (ValueError, struct.error) as e:
                    log.warning("#%d: bad frame: %s", c.n, e)
        except (ConnectionError, asyncio.IncompleteReadError, websockets.ConnectionClosed):
            pass
        finally:
            await self.leave(c)
            self.conn_count -= 1
            log.info("#%d %s: gone (%d remain)", c.n, c.peer, self.conn_count)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--ws-port", type=int, default=8643)
    ap.add_argument("--tcp-port", type=int, default=8644)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    relay = Relay()
    relay.start_cleanup()

    async def ws_handler(ws, path=None):
        if relay.conn_count >= MAX_CONNS:
            await ws.close(1013, "server full")
            return
        async def frames():
            async for m in ws:
                if isinstance(m, bytes):
                    yield m
        await relay.serve(Conn(ws.send, ws.remote_address), frames())

    async def tcp_handler(reader, writer):
        if relay.conn_count >= MAX_CONNS:
            writer.close()
            return
        async def send(b):
            writer.write(struct.pack("<I", len(b)) + b)
            await writer.drain()
        async def frames():
            while True:
                hdr = await reader.readexactly(4)
                n, = struct.unpack("<I", hdr)
                if n > MAX_FRAME:
                    raise ValueError(f"frame too large: {n}")
                yield await reader.readexactly(n)
        await relay.serve(Conn(send, writer.get_extra_info("peername")), frames())
        writer.close()

    async def process_request(path, headers):
        p = path.split("?")[0]
        if p.endswith("/health"):
            body = json.dumps({"ok": True, "rooms": len(relay.rooms), "connections": relay.conn_count}).encode()
            return (http.HTTPStatus.OK, [("Content-Type", "application/json"), ("Cache-Control", "no-store")], body)
        if not p.endswith("/rooms"):
            return None
        rooms = []
        for r in relay.rooms.values():
            flags = struct.unpack_from("<I", r.desc, 4)[0]
            rooms.append({
                "id": r.instance.hex(), "name": r.name.decode("cp1252", "replace"),
                "players": [p.short.decode("cp1252", "replace") for p in r.players.values()],
                "max": r.max_players(), "open": not flags & (DPSESSION_JOINDISABLED | DPSESSION_NEWPLAYERSDISABLED),
                "password": bool(r.password),
            })
        body = json.dumps({"rooms": rooms}).encode()
        return (http.HTTPStatus.OK, [("Content-Type", "application/json"), ("Cache-Control", "no-store"),
                      ("Access-Control-Allow-Origin", "*"), ("Cross-Origin-Resource-Policy", "cross-origin")], body)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async with websockets.serve(ws_handler, a.host, a.ws_port, max_size=MAX_FRAME, process_request=process_request):
        tcp = await asyncio.start_server(tcp_handler, a.host, a.tcp_port)
        log.info("relay: ws://%s:%d/relay (browser), tcp %s:%d (native)", a.host, a.ws_port, a.host, a.tcp_port)
        async with tcp:
            await stop.wait()
    log.info("relay stopped")


if __name__ == "__main__":
    asyncio.run(main())
