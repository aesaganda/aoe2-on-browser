#!/usr/bin/env python3
"""Pack, verify, and unpack turnkey prebuilt browser bundles for aoe2-on-browser.

A bundle is a single self-contained tar/tar.gz archive pairing the compiled WebAssembly
build (`build/web/aoe2.{js,json,wasm}`) with the matching game install tree
(`install/`), verified against `aoe2.json`'s `exe.sha256` and `exe.size`.

Subcommands:
    tools/bundle.py pack   [--build-dir DIR] [--install-dir DIR] [--out FILE]
    tools/bundle.py verify ARCHIVE [--sha256 HEX] [--json]
    tools/bundle.py unpack ARCHIVE [--build-dir DIR] [--install-dir DIR] [--sha256 HEX]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import errno
import gzip
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time

FORMAT_VERSION = 1
REQUIRED_BUILD_FILES = ("aoe2.js", "aoe2.json", "aoe2.wasm")
_CHUNK_SIZE = 1024 * 1024  # 1 MiB
_MAX_JSON_BYTES = 1024 * 1024


class BundleError(RuntimeError):
    """Raised when bundle creation, verification, or extraction fails."""


def _is_extra_build_file(name: str) -> bool:
    if name in REQUIRED_BUILD_FILES:
        return False
    if name.endswith(".gz") or ".tmp" in name:
        return False
    return name == "aoe2.data" or (name.startswith("aoe2.") and name.endswith(".js"))


def _skip_install_rel(rel: str, full_install: bool = False) -> bool:
    p = rel.replace("\\", "/").strip("/").lower()
    if not p:
        return True
    base = p.rsplit("/", 1)[-1]
    if base in (".ds_store", "thumbs.db"):
        return True
    if full_install:
        return False
    if p == "cdrom" or p.startswith("cdrom/") or p == "cdrom.label":
        return True
    if p.endswith(".avi") or p.endswith(".cab") or base == ".complete":
        return True
    if p == "registry.ini" or p == "windows" or p.startswith("windows/"):
        return True
    if p in ("player.nfp", "player.nfz"):
        return True
    if p.startswith("savegame/") and base.endswith((".gam", ".gax", ".mgx", ".mgz")):
        return True
    return False


def _sha256_file(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
    return h.hexdigest(), total


def _sha256_stream(stream, expected_size: int | None = None) -> tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    while True:
        chunk = stream.read(_CHUNK_SIZE)
        if not chunk:
            break
        h.update(chunk)
        total += len(chunk)
    if expected_size is not None and total != expected_size:
        raise BundleError(f"truncated stream: expected {expected_size} bytes, got {total}")
    return h.hexdigest(), total


def _validate_hex64(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise BundleError(f"{label} must be a 64-character hex string")
    norm = value.strip().lower()
    if len(norm) != 64:
        raise BundleError(f"{label} must be 64 hex characters (got {len(norm)})")
    try:
        int(norm, 16)
    except ValueError as exc:
        raise BundleError(f"{label} is not valid hexadecimal") from exc
    return norm


def _validate_aoe2_json(obj: object, label: str = "aoe2.json") -> tuple[str, int]:
    if not isinstance(obj, dict):
        raise BundleError(f"{label}: expected JSON object")
    exe = obj.get("exe")
    if not isinstance(exe, dict):
        raise BundleError(f"{label}: missing 'exe' object")
    sha = _validate_hex64(exe.get("sha256"), f"{label}.exe.sha256")
    size = exe.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise BundleError(f"{label}.exe.size must be a positive integer")
    return sha, size


def _find_install_exe(install_dir: str) -> str:
    exact = os.path.join(install_dir, "age2_x1", "age2_x1.exe")
    if os.path.isfile(exact):
        return exact

    # Check case-insensitively so we can report a helpful casing error.
    try:
        top_entries = os.listdir(install_dir)
    except OSError as exc:
        raise BundleError(f"cannot read install directory {install_dir}: {exc}") from exc

    for dname in top_entries:
        if dname.lower() == "age2_x1":
            sub = os.path.join(install_dir, dname)
            if os.path.isdir(sub):
                for fname in os.listdir(sub):
                    if fname.lower() == "age2_x1.exe":
                        found = os.path.join(sub, fname)
                        raise BundleError(
                            f"found executable at {found}, expected lowercase 'age2_x1/age2_x1.exe'"
                        )
    raise BundleError(f"missing {exact} (install directory does not look valid)")


def _validate_tar_member(member: tarfile.TarInfo) -> str:
    if not (member.isreg() or member.isdir()):
        raise BundleError(
            f"unsupported archive member type for {member.name!r} (only regular files and directories are allowed)"
        )
    raw = member.name
    if not raw or "\x00" in raw or "\\" in raw or raw.startswith("/"):
        raise BundleError(f"unsafe archive member path: {raw!r}")

    norm = raw
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.rstrip("/")
    if not norm:
        raise BundleError(f"unsafe archive member path: {raw!r}")

    parts = norm.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise BundleError(f"unsafe archive member path traversal: {raw!r}")

    if norm == "bundle.json":
        if not member.isreg():
            raise BundleError("bundle.json must be a regular file")
        return norm

    if norm in ("build", "build/web", "install"):
        if not member.isdir():
            raise BundleError(f"{norm} must be a directory")
        return norm

    if norm.startswith("build/web/"):
        if len(parts) != 3 or not member.isreg():
            raise BundleError(f"disallowed entry under build/web/: {raw!r}")
        return norm

    if norm.startswith("install/"):
        if len(parts) < 2:
            raise BundleError(f"disallowed entry under install/: {raw!r}")
        return norm

    raise BundleError(f"disallowed archive member path: {raw!r}")


def _gzip_wasm(wasm_path: str) -> str:
    st = os.stat(wasm_path)
    gz_path = wasm_path + ".gz"
    parent = os.path.dirname(os.path.abspath(wasm_path))
    fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".aoe2.wasm.gz.", suffix=".tmp")
    os.close(fd)
    try:
        with open(wasm_path, "rb") as src, open(tmp_path, "wb") as raw_dst:
            with gzip.GzipFile(
                filename="aoe2.wasm",
                mode="wb",
                compresslevel=6,
                fileobj=raw_dst,
                mtime=int(st.st_mtime),
            ) as gz_dst:
                shutil.copyfileobj(src, gz_dst, _CHUNK_SIZE)
        os.chmod(tmp_path, 0o644)
        os.utime(tmp_path, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(tmp_path, gz_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return gz_path


def _swap_in_dir(src_dir: str, dst_dir: str) -> str | None:
    src_abs = os.path.abspath(src_dir)
    dst_abs = os.path.abspath(dst_dir)
    os.makedirs(os.path.dirname(dst_abs), exist_ok=True)

    backup: str | None = None
    if os.path.lexists(dst_abs):
        backup = f"{dst_abs}.bak.{os.getpid()}.{int(time.time() * 1000)}"
        os.replace(dst_abs, backup)

    try:
        try:
            os.replace(src_abs, dst_abs)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            shutil.move(src_abs, dst_abs)
    except Exception:
        if os.path.lexists(dst_abs):
            shutil.rmtree(dst_abs, ignore_errors=True)
        if backup and os.path.lexists(backup):
            os.replace(backup, dst_abs)
        raise

    return backup


def _add_file_to_tar(tar: tarfile.TarFile, src_path: str, arcname: str) -> int:
    st = os.stat(src_path)
    info = tarfile.TarInfo(name=arcname)
    info.size = st.st_size
    info.mtime = int(st.st_mtime)
    info.mode = 0o644
    info.type = tarfile.REGTYPE
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    with open(src_path, "rb") as f:
        tar.addfile(info, f)
    return st.st_size


def _add_bytes_to_tar(tar: tarfile.TarFile, data: bytes, arcname: str, mtime: int | None = None) -> None:
    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    info.mtime = int(time.time()) if mtime is None else int(mtime)
    info.mode = 0o644
    info.type = tarfile.REGTYPE
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    tar.addfile(info, io.BytesIO(data))


def pack_bundle(
    build_dir: str,
    install_dir: str,
    out_path: str,
    full_install: bool = False,
    compress_level: int = 6,
    no_gzip: bool = False,
) -> dict[str, object]:
    if not (0 <= compress_level <= 9):
        raise BundleError(f"--compress-level must be between 0 and 9 (got {compress_level})")
    if not os.path.isdir(build_dir):
        raise BundleError(f"build directory not found: {build_dir}")
    if not os.path.isdir(install_dir):
        raise BundleError(f"install directory not found: {install_dir}")

    for name in REQUIRED_BUILD_FILES:
        fpath = os.path.join(build_dir, name)
        if not os.path.isfile(fpath) or os.path.getsize(fpath) <= 0:
            raise BundleError(f"missing or empty required build file: {fpath}")

    aoe2_json_path = os.path.join(build_dir, "aoe2.json")
    try:
        with open(aoe2_json_path, "r", encoding="utf-8") as f:
            aoe2_json = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"cannot parse {aoe2_json_path}: {exc}") from exc
    want_sha256, want_size = _validate_aoe2_json(aoe2_json)

    exe_path = _find_install_exe(install_dir)
    got_sha256, got_size = _sha256_file(exe_path)
    if got_sha256 != want_sha256 or got_size != want_size:
        raise BundleError(
            f"{exe_path} does not match {aoe2_json_path}: "
            f"got {got_sha256} ({got_size} bytes), "
            f"expected {want_sha256} ({want_size} bytes)"
        )

    build_names = list(REQUIRED_BUILD_FILES)
    for entry in sorted(os.listdir(build_dir)):
        full = os.path.join(build_dir, entry)
        if os.path.isfile(full) and not os.path.islink(full) and _is_extra_build_file(entry):
            build_names.append(entry)
    build_names = sorted(set(build_names))

    install_files: list[tuple[str, str]] = []
    for root, dirs, files in os.walk(install_dir):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(root, d)))
        for fname in sorted(files):
            full = os.path.join(root, fname)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, install_dir).replace(os.sep, "/")
            if _skip_install_rel(rel, full_install=full_install):
                continue
            install_files.append((rel, full))
    install_files.sort(key=lambda item: item[0])

    install_bytes = sum(os.path.getsize(full) for _, full in install_files)
    created_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    bundle_meta = {
        "format": FORMAT_VERSION,
        "created_at": created_at,
        "exe": {"sha256": got_sha256, "size": got_size},
        "build_files": build_names,
        "install_files": len(install_files),
        "install_bytes": install_bytes,
        "full_install": bool(full_install),
    }
    meta_bytes = (json.dumps(bundle_meta, indent=2) + "\n").encode("utf-8")

    out_abs = os.path.abspath(out_path)
    out_dir = os.path.dirname(out_abs)
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp_out = tempfile.mkstemp(dir=out_dir, prefix=".bundle.", suffix=".tmp")
    os.close(fd)

    try:
        open_kwargs = {"mode": "w"} if no_gzip else {"mode": "w:gz", "compresslevel": compress_level}
        with tarfile.open(tmp_out, **open_kwargs) as tar:
            _add_bytes_to_tar(tar, meta_bytes, "bundle.json")
            for bname in build_names:
                _add_file_to_tar(tar, os.path.join(build_dir, bname), f"build/web/{bname}")
            for rel, full in install_files:
                _add_file_to_tar(tar, full, f"install/{rel}")
        os.chmod(tmp_out, 0o644)
        os.replace(tmp_out, out_abs)
    except Exception:
        try:
            os.unlink(tmp_out)
        except OSError:
            pass
        raise

    arc_sha256, arc_size = _sha256_file(out_abs)
    sha_path = out_abs + ".sha256"
    sha_fd, sha_tmp = tempfile.mkstemp(dir=out_dir, prefix=".sha256.", suffix=".tmp")
    os.close(sha_fd)
    try:
        with open(sha_tmp, "w", encoding="utf-8") as f:
            f.write(f"{arc_sha256}  {os.path.basename(out_abs)}\n")
        os.chmod(sha_tmp, 0o644)
        os.replace(sha_tmp, sha_path)
    except Exception:
        try:
            os.unlink(sha_tmp)
        except OSError:
            pass
        raise

    return {
        **bundle_meta,
        "out": out_abs,
        "sha256_file": sha_path,
        "archive_sha256": arc_sha256,
        "archive_size": arc_size,
    }


def _read_member_json(tar: tarfile.TarFile, member: tarfile.TarInfo, label: str) -> object:
    if member.size > _MAX_JSON_BYTES:
        raise BundleError(f"{label} is unexpectedly large ({member.size} bytes)")
    stream = tar.extractfile(member)
    if stream is None:
        raise BundleError(f"cannot read {label} from archive")
    raw = stream.read(_MAX_JSON_BYTES + 1)
    if len(raw) != member.size or len(raw) > _MAX_JSON_BYTES:
        raise BundleError(f"invalid size while reading {label}")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"invalid JSON in {label}: {exc}") from exc


def verify_bundle(archive_path: str, expected_sha256: str | None = None) -> dict[str, object]:
    if not os.path.isfile(archive_path):
        raise BundleError(f"bundle archive not found: {archive_path}")

    arc_sha256, arc_size = _sha256_file(archive_path)
    if expected_sha256 is not None and expected_sha256.strip():
        want_arc_sha = _validate_hex64(expected_sha256, "--sha256")
        if arc_sha256 != want_arc_sha:
            raise BundleError(
                f"archive SHA-256 mismatch: got {arc_sha256}, expected {want_arc_sha}"
            )

    members: dict[str, tarfile.TarInfo] = {}
    try:
        with tarfile.open(archive_path, "r:*") as tar:
            for member in tar:
                norm = _validate_tar_member(member)
                if norm in members:
                    raise BundleError(f"duplicate archive member path: {norm}")
                members[norm] = member

            required = (
                "bundle.json",
                "build/web/aoe2.js",
                "build/web/aoe2.json",
                "build/web/aoe2.wasm",
                "install/age2_x1/age2_x1.exe",
            )
            for req in required:
                m = members.get(req)
                if m is None or not m.isreg() or m.size <= 0:
                    raise BundleError(f"missing or empty required member: {req}")

            bundle_meta = _read_member_json(tar, members["bundle.json"], "bundle.json")
            if not isinstance(bundle_meta, dict) or bundle_meta.get("format") != FORMAT_VERSION:
                raise BundleError(
                    f"unsupported bundle format: {bundle_meta.get('format') if isinstance(bundle_meta, dict) else type(bundle_meta).__name__}"
                )
            bundle_exe = _validate_aoe2_json(bundle_meta, "bundle.json")

            aoe2_meta = _read_member_json(tar, members["build/web/aoe2.json"], "build/web/aoe2.json")
            build_exe = _validate_aoe2_json(aoe2_meta, "build/web/aoe2.json")
            if bundle_exe != build_exe:
                raise BundleError("bundle.json exe metadata does not match build/web/aoe2.json")

            exe_member = members["install/age2_x1/age2_x1.exe"]
            exe_stream = tar.extractfile(exe_member)
            if exe_stream is None:
                raise BundleError("cannot read install/age2_x1/age2_x1.exe from archive")
            actual_sha256, actual_size = _sha256_stream(exe_stream, expected_size=exe_member.size)
            if (actual_sha256, actual_size) != build_exe:
                raise BundleError(
                    f"install/age2_x1/age2_x1.exe mismatch: got {actual_sha256} ({actual_size} bytes), "
                    f"expected {build_exe[0]} ({build_exe[1]} bytes)"
                )
    except tarfile.TarError as exc:
        raise BundleError(f"invalid or corrupted tar archive {archive_path}: {exc}") from exc

    build_files = sorted(
        p[len("build/web/"):]
        for p, m in members.items()
        if p.startswith("build/web/") and m.isreg()
    )
    install_reg = [m for p, m in members.items() if p.startswith("install/") and m.isreg()]
    return {
        "ok": True,
        "archive": os.path.abspath(archive_path),
        "archive_sha256": arc_sha256,
        "archive_size": arc_size,
        "format": bundle_meta["format"],
        "created_at": bundle_meta.get("created_at"),
        "exe": {"sha256": actual_sha256, "size": actual_size},
        "build_files": build_files,
        "install_files": len(install_reg),
        "install_bytes": sum(m.size for m in install_reg),
        "full_install": bool(bundle_meta.get("full_install", False)),
        "_bundle_meta": bundle_meta,
    }


def _safe_stage_target(stage_root: str, rel: str) -> str:
    root_real = os.path.realpath(stage_root)
    target = os.path.realpath(os.path.join(root_real, rel))
    if target != root_real and not target.startswith(root_real + os.sep):
        raise BundleError(f"path escapes staging directory: {rel!r}")
    return target


def unpack_bundle(
    archive_path: str,
    build_dir: str,
    install_dir: str,
    expected_sha256: str | None = None,
    no_gzip_wasm: bool = False,
    source: str | None = None,
) -> dict[str, object]:
    summary = verify_bundle(archive_path, expected_sha256=expected_sha256)
    bundle_meta = dict(summary.pop("_bundle_meta"))

    stage_root = tempfile.mkdtemp(prefix="aoe2-unpack.")
    build_backup: str | None = None
    install_backup: str | None = None
    try:
        stage_build = os.path.join(stage_root, "build_web")
        stage_install = os.path.join(stage_root, "install")
        os.makedirs(stage_build, exist_ok=True)
        os.makedirs(stage_install, exist_ok=True)

        with tarfile.open(archive_path, "r:*") as tar:
            for member in tar:
                norm = _validate_tar_member(member)
                if norm in ("bundle.json", "build", "build/web", "install"):
                    continue
                if norm.startswith("build/web/"):
                    target = _safe_stage_target(stage_build, norm[len("build/web/"):])
                elif norm.startswith("install/"):
                    target = _safe_stage_target(stage_install, norm[len("install/"):])
                else:
                    raise BundleError(f"unexpected member path: {norm}")

                if member.isdir():
                    os.makedirs(target, exist_ok=True)
                    continue

                os.makedirs(os.path.dirname(target), exist_ok=True)
                src_stream = tar.extractfile(member)
                if src_stream is None:
                    raise BundleError(f"cannot extract {norm}")
                with open(target, "wb") as dst:
                    shutil.copyfileobj(src_stream, dst, _CHUNK_SIZE)
                if os.path.getsize(target) != member.size:
                    raise BundleError(f"extracted size mismatch for {norm}")
                os.chmod(target, 0o644)
                if member.mtime and member.mtime > 0:
                    os.utime(target, (member.mtime, member.mtime))

        wasm_path = os.path.join(stage_build, "aoe2.wasm")
        wasm_gz_path = wasm_path + ".gz"
        if not no_gzip_wasm and not os.path.isfile(wasm_gz_path):
            _gzip_wasm(wasm_path)

        stamp = {
            **bundle_meta,
            "archive_sha256": summary["archive_sha256"],
            "unpacked_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if source:
            stamp["source"] = source
        stamp_path = os.path.join(stage_build, ".bundle.json")
        with open(stamp_path, "w", encoding="utf-8") as f:
            json.dump(stamp, f, indent=2)
            f.write("\n")

        build_backup = _swap_in_dir(stage_build, build_dir)
        try:
            install_backup = _swap_in_dir(stage_install, install_dir)
        except Exception:
            # Roll back build_dir so both directories remain from the same generation.
            if os.path.lexists(build_dir):
                shutil.rmtree(build_dir, ignore_errors=True)
            if build_backup and os.path.lexists(build_backup):
                os.replace(build_backup, build_dir)
                build_backup = None
            raise

        for bk in (build_backup, install_backup):
            if bk and os.path.lexists(bk):
                shutil.rmtree(bk, ignore_errors=True)
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)

    return {
        **summary,
        "build_dir": os.path.abspath(build_dir),
        "install_dir": os.path.abspath(install_dir),
    }


def _fmt_mib(nbytes: int) -> str:
    return f"{nbytes / (1024 * 1024):.1f} MiB"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pack, verify, and unpack prebuilt aoe2-on-browser bundles.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_pack = sub.add_parser("pack", help="Create a turnkey bundle archive from build/web and install/")
    p_pack.add_argument("--build-dir", default="build/web", help="Path to build/web directory")
    p_pack.add_argument("--install-dir", default="install", help="Path to install directory")
    p_pack.add_argument("--out", default="aoe2-bundle.tar.gz", help="Output archive path")
    p_pack.add_argument("--full-install", action="store_true", help="Include cdrom/, .avi, and .cab files too")
    p_pack.add_argument("--compress-level", type=int, default=6, help="Gzip level 0..9 (default: 6)")
    p_pack.add_argument("--no-gzip", action="store_true", help="Write an uncompressed .tar archive")

    p_verify = sub.add_parser("verify", help="Verify bundle structure, checksums, and exe match")
    p_verify.add_argument("archive", help="Path to bundle .tar.gz or .tar")
    p_verify.add_argument("--sha256", default=None, help="Expected SHA-256 hex digest of the archive")
    p_verify.add_argument("--json", action="store_true", help="Print verification summary as JSON")

    p_unpack = sub.add_parser("unpack", help="Verify and atomically unpack a bundle into build/web and install/")
    p_unpack.add_argument("archive", help="Path to bundle .tar.gz or .tar")
    p_unpack.add_argument("--build-dir", default="build/web", help="Destination build/web directory")
    p_unpack.add_argument("--install-dir", default="install", help="Destination install directory")
    p_unpack.add_argument("--sha256", default=None, help="Expected SHA-256 hex digest of the archive")
    p_unpack.add_argument("--no-gzip-wasm", action="store_true", help="Skip generating aoe2.wasm.gz")
    p_unpack.add_argument("--source", default=None, help="Optional source URL/path recorded in .bundle.json")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "pack":
            res = pack_bundle(
                build_dir=args.build_dir,
                install_dir=args.install_dir,
                out_path=args.out,
                full_install=args.full_install,
                compress_level=args.compress_level,
                no_gzip=args.no_gzip,
            )
            print(
                f"Packed {res['out']} ({_fmt_mib(res['archive_size'])}, "
                f"{res['install_files']} install files, {_fmt_mib(res['install_bytes'])} raw)"
            )
            print(f"SHA-256: {res['archive_sha256']} (wrote {res['sha256_file']})")
            print(f"Run on another machine: ./play.sh --bundle <URL> --bundle-sha256 {res['archive_sha256']}")
            return 0

        if args.cmd == "verify":
            res = verify_bundle(args.archive, expected_sha256=args.sha256)
            res.pop("_bundle_meta", None)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(
                    f"OK: {res['archive']} ({_fmt_mib(res['archive_size'])}, "
                    f"exe {res['exe']['sha256'][:12]}..., "
                    f"{len(res['build_files'])} build files, "
                    f"{res['install_files']} install files)"
                )
            return 0

        if args.cmd == "unpack":
            res = unpack_bundle(
                archive_path=args.archive,
                build_dir=args.build_dir,
                install_dir=args.install_dir,
                expected_sha256=args.sha256,
                no_gzip_wasm=args.no_gzip_wasm,
                source=args.source,
            )
            print(
                f"Unpacked {res['install_files']} install files -> {res['install_dir']} "
                f"and {len(res['build_files'])} build files -> {res['build_dir']}"
            )
            return 0
    except BundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
