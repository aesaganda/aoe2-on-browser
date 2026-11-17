#!/usr/bin/env python3
"""Read an Age of Empires II: The Conquerors 1.0 recorded game (.mgx, "VER 9.4").

    mgx.py <file.mgx>                 summary: ops, game time, commands, checksums
    mgx.py --checksums <file.mgx>     how many sync ops carry a checksum block
    mgx.py --duration <file.mgx>      game time in milliseconds
    mgx.py --last-checksum <file.mgx> game time of the last checksum, ms
    mgx.py --corrupt N <in> <out>     a copy whose N-th checksum is wrong (a control)

Layout, as the game writes it (0x426990 / 0x426F90):
  int32 header end, written when the recording closes (0 until then),
  int32 0, then the header (raw deflate: "VER 9.4", the game settings, the map, ...),
  then the body:
  - int32 log version (3), and the game start op 0x1F4 with 20 bytes;
  - op 1, a command: int32 length, the command, int32;
  - op 2, a sync: int32 time step (ms), int32 flag. A flag of 0 is followed by a
    28-byte checksum block (the world's checksums and its time); then 12 bytes of
    view position;
  - op 4, a chat: int32 -1, int32 length, text.
"""
import collections
import struct
import sys
import zlib


def body(path):
    d = open(path, "rb").read()
    z = zlib.decompressobj(-15)
    z.decompress(d[8:])
    return d, len(d) - len(z.unused_data), z.unused_data


def parse(path):
    _d, start, b = body(path)
    o, t = 4, 0
    ops, cmds, chk, err = collections.Counter(), collections.Counter(), [], None
    while o + 4 <= len(b):
        typ, = struct.unpack_from("<i", b, o)
        o += 4
        if typ == 0x1F4:
            o += 20
        elif typ == 1:
            n, = struct.unpack_from("<i", b, o)
            o += 4
            cmds[b[o]] += 1
            o += n + 4
        elif typ == 2:
            dt, flag = struct.unpack_from("<ii", b, o)
            o += 8
            t += dt
            if flag == 0:
                chk.append((t, start + o, b[o:o + 28]))
                o += 28
            o += 12
        elif typ == 4:
            code, = struct.unpack_from("<i", b, o)
            o += 4
            if code != -1:
                err = f"meta {code:#x} at {o}"
                break
            n, = struct.unpack_from("<i", b, o)
            o += 4 + n
        else:
            err = f"unknown op {typ} at body offset {o}, game time {t}"
            break
        ops[typ] += 1
    return dict(ops=dict(ops), time=t, cmds=cmds, chk=chk, err=err, end=o, size=len(b))


def main(argv):
    if argv[:1] == ["--checksums"]:
        print(len(parse(argv[1])["chk"]))
    elif argv[:1] == ["--last-checksum"]:
        print(parse(argv[1])["chk"][-1][0])
    elif argv[:1] == ["--duration"]:
        print(parse(argv[1])["time"])
    elif argv[:1] == ["--corrupt"]:
        n, src, dst = int(argv[1]), argv[2], argv[3]
        d = bytearray(open(src, "rb").read())
        _t, at, _blk = parse(src)["chk"][n - 1]
        d[at + 4] ^= 0xFF                         # the first checksum word
        open(dst, "wb").write(d)
    else:
        r = parse(argv[0])
        print(f"ops {r['ops']}, game time {r['time'] / 1000:.1f} s, parsed {r['end']} of {r['size']} body bytes"
              + (f", stopped: {r['err']}" if r["err"] else ""))
        print("commands by type:", {hex(k): v for k, v in sorted(r["cmds"].items())})
        print(f"checksum syncs: {len(r['chk'])}")
        for t, _at, blk in r["chk"]:
            print(f"  {t / 1000:7.1f} s  {blk.hex()}")


if __name__ == "__main__":
    main(sys.argv[1:])
