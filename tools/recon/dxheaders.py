"""Parse GUIDs and COM interface method tables out of the mingw-w64 DirectX headers.

We use the headers as the authoritative source instead of hard-coding GUIDs and
vtable layouts from memory. On Ubuntu they come from the `mingw-w64` package.
"""
from __future__ import annotations

import glob
import re
import uuid
from pathlib import Path

HEADER_DIRS = [
    "/usr/i686-w64-mingw32/include",
    "/usr/share/mingw-w64/include",
]
HEADERS = ["ddraw.h", "dsound.h", "dplay.h", "dplobby.h", "unknwn.h", "objbase.h", "d3d.h", "dinput.h"]

_GUID_RE = re.compile(
    r"DEFINE_GUID\s*\(\s*(\w+)\s*,\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*,"
    r"\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*,"
    r"\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*\)"
)
_IFACE_RE = re.compile(r"DECLARE_INTERFACE_\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)\s*\{(.*?)\n\}", re.S)
_METHOD_RE = re.compile(r"STDMETHOD(?:_\s*\(\s*[^,]+,\s*(\w+)\s*\)|\s*\(\s*(\w+)\s*\))\s*\((.*?)\)\s*PURE", re.S)


def find_header_dir() -> Path | None:
    for d in HEADER_DIRS:
        if Path(d, "ddraw.h").exists():
            return Path(d)
    return None


def guid_bytes(parts: list[int]) -> bytes:
    d1, d2, d3, *d4 = parts
    return d1.to_bytes(4, "little") + d2.to_bytes(2, "little") + d3.to_bytes(2, "little") + bytes(d4)


def load(hdir: Path | None = None):
    """Return (guids: {bytes16: name}, interfaces: {name: [(method, nargs_incl_this)]})."""
    hdir = hdir or find_header_dir()
    guids: dict[bytes, str] = {}
    ifaces: dict[str, list[tuple[str, int]]] = {}
    if hdir is None:
        return guids, ifaces
    texts = {}
    for h in HEADERS:
        p = hdir / h
        if p.exists():
            texts[h] = p.read_text(errors="replace")
    for text in texts.values():
        for m in _GUID_RE.finditer(text):
            name = m.group(1)
            parts = [int(x, 16) for x in m.groups()[1:]]
            guids.setdefault(guid_bytes(parts), name)
    raw: dict[str, tuple[str, list[tuple[str, int]]]] = {}
    for text in texts.values():
        for m in _IFACE_RE.finditer(text):
            name, base, body = m.group(1), m.group(2), m.group(3)
            methods = []
            for mm in _METHOD_RE.finditer(body):
                mname = mm.group(1) or mm.group(2)
                args = mm.group(3).strip()
                if args in ("THIS", ""):
                    n = 1
                else:
                    # THIS_ a, b, c  -> this + 3
                    rest = re.sub(r"^THIS_\s*", "", args)
                    n = 1 + len([a for a in _split_args(rest) if a.strip()])
                methods.append((mname, n))
            if methods:
                raw.setdefault(name, (base, methods))
    # The mingw headers spell out IUnknown's three methods in every interface, so
    # the method list already *is* the full vtable.
    for name, (_base, methods) in raw.items():
        ifaces[name] = methods
    return guids, ifaces


def _split_args(s: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def guid_str(b: bytes) -> str:
    return str(uuid.UUID(bytes_le=b)).upper()


if __name__ == "__main__":
    g, i = load()
    print(len(g), "guids", len(i), "interfaces")
    for k in ("IDirectDraw", "IDirectDrawSurface", "IDirectSoundBuffer", "IDirectPlay4"):
        print(k, i.get(k, [])[:8])
