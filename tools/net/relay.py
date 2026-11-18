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
"""
import argparse
import asyncio
import itertools
import logging
import os
import struct

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


class Reader:
    def __init__(self, b):
        self.b, self.o = b, 0

    def u8(self):
        v = self.b[self.o]; self.o += 1; return v

    def u32(self):
        v, = struct.unpack_from("<I", self.b, self.o); self.o += 4; return v

    def raw(self, n):
        v = self.b[self.o:self.o + n]; self.o += n; return bytes(v)

    def str(self):
        n, = struct.unpack_from("<H", self.b, self.o); self.o += 2; return self.raw(n)

    def blob(self):
        return self.raw(self.u32())


def s(v):
    return struct.pack("<H", len(v)) + v


def blob(v):
    return struct.pack("<I", len(v)) + v


class Player:
    def __init__(self, pid, conn, flags, short, long_, data):
        self.id, self.conn, self.flags, self.short, self.long, self.data = pid, conn, flags, short, long_, data

    def pack(self):
        return struct.pack("<II", self.id, self.flags) + s(self.short) + s(self.long) + blob(self.data)


class Room:
    def __init__(self, desc, name, password, host):
        self.desc = bytearray(desc)                  # DPSESSIONDESC2, 80 bytes
        self.name, self.password, self.host = name, password, host
        self.players = {}                            # id -> Player
        self.conns = {host}

    @property
    def instance(self):
        return bytes(self.desc[8:24])

    def max_players(self):
        return struct.unpack_from("<I", self.desc, 40)[0]

    def pack(self):
        struct.pack_into("<I", self.desc, 44, len(self.players))   # dwCurrentPlayers
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
        self.rooms = {}                              # instance guid -> Room
        self.pids = itertools.count(0x10000 + (int.from_bytes(os.urandom(2), "little") << 8))

    async def reply(self, c, req, hr, body=b""):
        await c.send(struct.pack("<BIII", REPLY, 0, req, hr) + body)

    async def to_room(self, room, frame, but=None):
        for c in list(room.conns):
            if c is not but:
                await c.send(frame)

    async def handle(self, c, frame):
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
            await self.leave(c)
            desc, name, password = bytearray(r.raw(80)), r.str(), r.str()
            desc[8:24] = os.urandom(16)               # guidInstance
            room = Room(desc, name, password, c)
            self.rooms[room.instance] = room
            c.room = room
            log.info("#%d created room %r (max %d)", c.n, name, room.max_players())
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
        if t == NEWPLAYER:
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
                await p.conn.send(struct.pack("<BIII", MSG, 0, src, p.id) + blob(data))
            return                                    # sends are not answered
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
            if room.conns:                            # the host migrates (DPSESSION_MIGRATEHOST games)
                room.host = next(iter(room.conns))
                await room.host.send(struct.pack("<BI", SYS_HOST, 0))
            else:
                self.rooms.pop(room.instance, None)
                log.info("room %r closed", room.name)

    async def serve(self, c, frames):
        try:
            async for f in frames:
                await self.handle(c, f)
        except (ConnectionError, asyncio.IncompleteReadError, websockets.ConnectionClosed):
            pass
        finally:
            await self.leave(c)
            log.info("#%d %s: gone", c.n, c.peer)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--ws-port", type=int, default=8643)
    ap.add_argument("--tcp-port", type=int, default=8644)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    relay = Relay()

    async def ws_handler(ws, path=None):
        async def frames():
            async for m in ws:
                if isinstance(m, bytes):
                    yield m
        await relay.serve(Conn(ws.send, ws.remote_address), frames())

    async def tcp_handler(reader, writer):
        async def send(b):
            writer.write(struct.pack("<I", len(b)) + b)
            await writer.drain()

        async def frames():
            while True:
                n, = struct.unpack("<I", await reader.readexactly(4))
                yield await reader.readexactly(n)
        await relay.serve(Conn(send, writer.get_extra_info("peername")), frames())
        writer.close()

    async with websockets.serve(ws_handler, a.host, a.ws_port, max_size=1 << 20):
        tcp = await asyncio.start_server(tcp_handler, a.host, a.tcp_port)
        log.info("relay: ws://%s:%d/relay (browser), tcp %s:%d (native)", a.host, a.ws_port, a.host, a.tcp_port)
        async with tcp:
            await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
