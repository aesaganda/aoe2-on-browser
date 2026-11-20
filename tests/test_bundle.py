"""Unit and integration tests for tools/bundle.py and play.sh turnkey bundle flow."""

import gzip
import hashlib
import http.server
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "web"))

import bundle  # noqa: E402
import serve  # noqa: E402


def _write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _create_synthetic_tree(base_dir):
    build_dir = os.path.join(base_dir, "build", "web")
    install_dir = os.path.join(base_dir, "install")

    exe_bytes = b"MZ" + b"AGE2_X1_SYNTHETIC_EXE" * 200
    exe_sha = hashlib.sha256(exe_bytes).hexdigest()
    exe_size = len(exe_bytes)

    wasm_bytes = b"\x00asm\x01\x00\x00\x00" + b"WASM-PAYLOAD-" * 64
    _write_bytes(os.path.join(build_dir, "aoe2.wasm"), wasm_bytes)
    _write_bytes(os.path.join(build_dir, "aoe2.wasm.gz"), b"stale-gz-must-not-be-packed")
    _write_bytes(os.path.join(build_dir, "aoe2.js"), b"var AoE2 = {};\n")
    _write_bytes(
        os.path.join(build_dir, "aoe2.json"),
        json.dumps({"exe": {"sha256": exe_sha, "size": exe_size}}).encode("utf-8"),
    )
    _write_bytes(os.path.join(build_dir, "aoe2.worker.js"), b"// extra worker js\n")
    _write_bytes(os.path.join(build_dir, "obj", "app", "main.o"), b"OBJECT")

    # Required / runtime install assets
    _write_bytes(os.path.join(install_dir, "age2_x1", "age2_x1.exe"), exe_bytes)
    _write_bytes(os.path.join(install_dir, "data", "empires2_x1.dat"), b"DAT-CONTENT")
    _write_bytes(os.path.join(install_dir, "sound", "taunt01.wav"), b"RIFF-WAV")
    _write_bytes(os.path.join(install_dir, "avi", "introx.mpg"), b"MPEG1-DATA")
    _write_bytes(os.path.join(install_dir, "savegame", "scenario.scx"), b"SCENARIO")

    # Files that must be skipped in trimmed browser bundles
    _write_bytes(os.path.join(install_dir, "cdrom", "setup.exe"), b"CDROM-SKIP")
    _write_bytes(os.path.join(install_dir, "cdrom.label"), b"AGE2_X1\n")
    _write_bytes(os.path.join(install_dir, "avi", "introx.avi"), b"AVI-SKIP")
    _write_bytes(os.path.join(install_dir, "setup.cab"), b"CAB-SKIP")
    _write_bytes(os.path.join(install_dir, ".complete"), b"COMPLETE-SKIP")
    _write_bytes(os.path.join(install_dir, "registry.ini"), b"REG-SKIP")
    _write_bytes(os.path.join(install_dir, "windows", "temp", "map800.bmp"), b"TEMP-SKIP")
    _write_bytes(os.path.join(install_dir, ".DS_Store"), b"MAC-JUNK")
    _write_bytes(os.path.join(install_dir, "player.nfp"), b"PLAYER-PROFILE")
    _write_bytes(os.path.join(install_dir, "savegame", "test.mgx"), b"SAVED-REC")

    return {
        "build_dir": build_dir,
        "install_dir": install_dir,
        "exe_bytes": exe_bytes,
        "exe_sha": exe_sha,
        "exe_size": exe_size,
        "wasm_bytes": wasm_bytes,
    }


def _build_custom_tar(path, entries):
    """Create a .tar.gz with arbitrary TarInfo entries for negative security tests."""
    with tarfile.open(path, "w:gz") as tar:
        for info, payload in entries:
            if payload is None:
                tar.addfile(info)
            else:
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))


class TestBundleTool(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = self._tmp.name
        self.src_dir = os.path.join(self.tmp_dir, "src")
        self.tree = _create_synthetic_tree(self.src_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_pack_verify_unpack_round_trip(self):
        out_arc = os.path.join(self.tmp_dir, "out", "aoe2-bundle.tar.gz")
        packed = bundle.pack_bundle(
            build_dir=self.tree["build_dir"],
            install_dir=self.tree["install_dir"],
            out_path=out_arc,
        )
        self.assertTrue(os.path.isfile(out_arc))
        self.assertTrue(os.path.isfile(out_arc + ".sha256"))
        with open(out_arc + ".sha256", "r", encoding="utf-8") as f:
            sha_line = f.read().strip()
        self.assertEqual(sha_line, f"{packed['archive_sha256']}  aoe2-bundle.tar.gz")

        with tarfile.open(out_arc, "r:gz") as tar:
            names = set(tar.getnames())

        self.assertIn("bundle.json", names)
        self.assertIn("build/web/aoe2.js", names)
        self.assertIn("build/web/aoe2.json", names)
        self.assertIn("build/web/aoe2.wasm", names)
        self.assertIn("build/web/aoe2.worker.js", names)
        self.assertNotIn("build/web/aoe2.wasm.gz", names)
        self.assertNotIn("build/web/obj/app/main.o", names)

        self.assertIn("install/age2_x1/age2_x1.exe", names)
        self.assertIn("install/data/empires2_x1.dat", names)
        self.assertIn("install/avi/introx.mpg", names)
        self.assertIn("install/savegame/scenario.scx", names)
        for skipped in (
            "install/cdrom/setup.exe",
            "install/cdrom.label",
            "install/avi/introx.avi",
            "install/setup.cab",
            "install/.complete",
            "install/registry.ini",
            "install/windows/temp/map800.bmp",
            "install/.DS_Store",
            "install/player.nfp",
            "install/savegame/test.mgx",
        ):
            self.assertNotIn(skipped, names)

        verified = bundle.verify_bundle(out_arc, expected_sha256=packed["archive_sha256"].upper())
        self.assertTrue(verified["ok"])
        self.assertEqual(verified["exe"]["sha256"], self.tree["exe_sha"])
        self.assertEqual(verified["exe"]["size"], self.tree["exe_size"])

        dst_build = os.path.join(self.tmp_dir, "dst", "build", "web")
        dst_install = os.path.join(self.tmp_dir, "dst", "install")
        unpacked = bundle.unpack_bundle(
            out_arc,
            build_dir=dst_build,
            install_dir=dst_install,
            expected_sha256=packed["archive_sha256"],
            source="https://example.invalid/aoe2-bundle.tar.gz",
        )
        self.assertEqual(unpacked["install_files"], 5)

        wasm_path = os.path.join(dst_build, "aoe2.wasm")
        wasm_gz_path = os.path.join(dst_build, "aoe2.wasm.gz")
        self.assertTrue(os.path.isfile(wasm_gz_path))
        self.assertEqual(int(os.stat(wasm_gz_path).st_mtime), int(os.stat(wasm_path).st_mtime))
        with open(wasm_gz_path, "rb") as f:
            self.assertEqual(gzip.decompress(f.read()), self.tree["wasm_bytes"])

        with open(os.path.join(dst_build, ".bundle.json"), "r", encoding="utf-8") as f:
            stamp = json.load(f)
        self.assertEqual(stamp["archive_sha256"], packed["archive_sha256"])
        self.assertEqual(stamp["source"], "https://example.invalid/aoe2-bundle.tar.gz")

        entries = serve.bundle_index(dst_install)
        rels = {rel for rel, _, _, _ in entries}
        self.assertEqual(
            rels,
            {
                "age2_x1/age2_x1.exe",
                "avi/introx.mpg",
                "data/empires2_x1.dat",
                "savegame/scenario.scx",
                "sound/taunt01.wav",
            },
        )

    def test_pack_full_install_keeps_disc_and_avi_files(self):
        out_arc = os.path.join(self.tmp_dir, "full.tar.gz")
        bundle.pack_bundle(
            build_dir=self.tree["build_dir"],
            install_dir=self.tree["install_dir"],
            out_path=out_arc,
            full_install=True,
        )
        with tarfile.open(out_arc, "r:gz") as tar:
            names = set(tar.getnames())
        self.assertIn("install/cdrom/setup.exe", names)
        self.assertIn("install/cdrom.label", names)
        self.assertIn("install/avi/introx.avi", names)
        self.assertNotIn("install/.DS_Store", names)

    def test_pack_rejects_missing_or_mismatched_files(self):
        out_arc = os.path.join(self.tmp_dir, "bad.tar.gz")
        # Mismatch between aoe2.json and install/age2_x1/age2_x1.exe
        _write_bytes(os.path.join(self.tree["install_dir"], "age2_x1", "age2_x1.exe"), b"DIFFERENT-EXE")
        with self.assertRaises(bundle.BundleError):
            bundle.pack_bundle(self.tree["build_dir"], self.tree["install_dir"], out_arc)
        self.assertFalse(os.path.exists(out_arc))

        # Missing required build file
        os.unlink(os.path.join(self.tree["build_dir"], "aoe2.wasm"))
        with self.assertRaises(bundle.BundleError):
            bundle.pack_bundle(self.tree["build_dir"], self.tree["install_dir"], out_arc)

    def _valid_base_tar_entries(self):
        exe_bytes = self.tree["exe_bytes"]
        exe_sha = self.tree["exe_sha"]
        exe_size = self.tree["exe_size"]
        meta = {
            "format": 1,
            "created_at": "2026-11-20T00:00:00Z",
            "exe": {"sha256": exe_sha, "size": exe_size},
            "build_files": ["aoe2.js", "aoe2.json", "aoe2.wasm"],
            "install_files": 1,
            "install_bytes": exe_size,
            "full_install": False,
        }
        aoe2_json = {"exe": {"sha256": exe_sha, "size": exe_size}}

        def reg(name, data):
            ti = tarfile.TarInfo(name)
            ti.type = tarfile.REGTYPE
            return ti, data

        return [
            reg("bundle.json", json.dumps(meta).encode("utf-8")),
            reg("build/web/aoe2.js", b"var AoE2 = {};"),
            reg("build/web/aoe2.json", json.dumps(aoe2_json).encode("utf-8")),
            reg("build/web/aoe2.wasm", self.tree["wasm_bytes"]),
            reg("install/age2_x1/age2_x1.exe", exe_bytes),
        ]

    def test_verify_and_unpack_reject_unsafe_or_tampered_archives(self):
        good_arc = os.path.join(self.tmp_dir, "good.tar.gz")
        bundle.pack_bundle(self.tree["build_dir"], self.tree["install_dir"], good_arc)
        with self.assertRaises(bundle.BundleError):
            bundle.verify_bundle(good_arc, expected_sha256="0" * 64)

        bad_cases = []

        # Path traversal
        t_trav = tarfile.TarInfo("install/../../evil.txt")
        bad_cases.append(("traversal", [(t_trav, b"bad")]))

        # Absolute path
        t_abs = tarfile.TarInfo("/etc/passwd")
        bad_cases.append(("absolute", [(t_abs, b"bad")]))

        # Symlink member
        t_sym = tarfile.TarInfo("install/link")
        t_sym.type = tarfile.SYMTYPE
        t_sym.linkname = "/etc/passwd"
        bad_cases.append(("symlink", [(t_sym, None)]))

        # Unexpected top-level path
        t_other = tarfile.TarInfo("other/foo")
        bad_cases.append(("unexpected", [(t_other, b"bad")]))

        for label, extra in bad_cases:
            with self.subTest(case=label):
                arc = os.path.join(self.tmp_dir, f"bad-{label}.tar.gz")
                _build_custom_tar(arc, self._valid_base_tar_entries() + extra)
                with self.assertRaises(bundle.BundleError):
                    bundle.verify_bundle(arc)

        # Tampered age2_x1.exe inside archive
        tampered = os.path.join(self.tmp_dir, "tampered-exe.tar.gz")
        entries = self._valid_base_tar_entries()
        ti_exe, _ = entries[-1]
        entries[-1] = (ti_exe, b"TAMPERED" * (len(self.tree["exe_bytes"]) // 8 + 1))
        _build_custom_tar(tampered, entries)
        with self.assertRaises(bundle.BundleError):
            bundle.verify_bundle(tampered)

        # Atomic preservation when unpack fails on tampered archive
        dst_build = os.path.join(self.tmp_dir, "preserve", "build", "web")
        dst_install = os.path.join(self.tmp_dir, "preserve", "install")
        _write_bytes(os.path.join(dst_build, "marker.txt"), b"keep-build")
        _write_bytes(os.path.join(dst_install, "marker.txt"), b"keep-install")
        with self.assertRaises(bundle.BundleError):
            bundle.unpack_bundle(tampered, build_dir=dst_build, install_dir=dst_install)
        with open(os.path.join(dst_build, "marker.txt"), "rb") as f:
            self.assertEqual(f.read(), b"keep-build")
        with open(os.path.join(dst_install, "marker.txt"), "rb") as f:
            self.assertEqual(f.read(), b"keep-install")


class TestPlayShBundleIntegration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = self._tmp.name
        self.repo_dir = os.path.join(self.tmp_dir, "repo")
        os.makedirs(os.path.join(self.repo_dir, "tools"), exist_ok=True)
        shutil.copy2(os.path.join(ROOT, "play.sh"), os.path.join(self.repo_dir, "play.sh"))
        os.chmod(os.path.join(self.repo_dir, "play.sh"), 0o755)
        shutil.copy2(os.path.join(ROOT, "tools", "bundle.py"), os.path.join(self.repo_dir, "tools", "bundle.py"))
        os.chmod(os.path.join(self.repo_dir, "tools", "bundle.py"), 0o755)
        shutil.copy2(os.path.join(ROOT, ".env.example"), os.path.join(self.repo_dir, ".env.example"))

        # Stub failing docker on PATH to prove --pack and --bundle --build-only work without Docker
        self.fake_bin = os.path.join(self.tmp_dir, "fake_bin")
        os.makedirs(self.fake_bin, exist_ok=True)
        fake_docker = os.path.join(self.fake_bin, "docker")
        with open(fake_docker, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho 'docker should not be required here' >&2\nexit 1\n")
        os.chmod(fake_docker, stat.S_IRWXU)

        py_dir = os.path.dirname(sys.executable)
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.fake_bin}:{py_dir}:{self.env.get('PATH', '')}"
        for k in ("AOE2_BUNDLE_URL", "AOE2_BUNDLE_SHA256", "AOE2_BUNDLE_TOKEN", "AOE2_BUNDLE_HEADER"):
            self.env.pop(k, None)

    def tearDown(self):
        self._tmp.cleanup()

    def _run_play(self, args, extra_env=None):
        env = self.env.copy()
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", os.path.join(self.repo_dir, "play.sh"), *args],
            cwd=self.repo_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_syntax_and_help(self):
        res_n = subprocess.run(["bash", "-n", os.path.join(self.repo_dir, "play.sh")], capture_output=True, text=True)
        self.assertEqual(res_n.returncode, 0, res_n.stderr)

        res_h = self._run_play(["--help"])
        self.assertEqual(res_h.returncode, 0, res_h.stderr)
        for flag in ("--bundle", "--bundle-sha256", "--refresh-bundle", "--pack"):
            self.assertIn(flag, res_h.stdout)

    def test_pack_http_bundle_cache_refresh_and_local_bundle(self):
        # Create a tree in a separate pack workspace and pack it via play.sh --pack
        pack_repo = os.path.join(self.tmp_dir, "pack_repo")
        shutil.copytree(self.repo_dir, pack_repo)
        tree = _create_synthetic_tree(pack_repo)
        bundle_file = os.path.join(pack_repo, "aoe2-bundle.tar.gz")

        res_pack = subprocess.run(
            ["bash", os.path.join(pack_repo, "play.sh"), "--pack", bundle_file],
            cwd=pack_repo,
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_pack.returncode, 0, res_pack.stderr + res_pack.stdout)
        self.assertTrue(os.path.isfile(bundle_file))
        self.assertTrue(os.path.isfile(bundle_file + ".sha256"))

        with open(bundle_file, "rb") as f:
            bundle_bytes = f.read()
        bundle_sha = hashlib.sha256(bundle_bytes).hexdigest()

        # Start local HTTP server requiring Authorization: Bearer secret-tok
        state = {"count": 0, "auths": []}

        class AuthBundleHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                state["count"] += 1
                state["auths"].append(self.headers.get("Authorization"))
                if self.headers.get("Authorization") != "Bearer secret-tok":
                    self.send_response(401)
                    self.end_headers()
                    return
                if self.path != "/aoe2-bundle.tar.gz":
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Length", str(len(bundle_bytes)))
                self.end_headers()
                self.wfile.write(bundle_bytes)

            def log_message(self, format, *args):  # noqa: A002
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), AuthBundleHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            url = f"http://127.0.0.1:{srv.server_port}/aoe2-bundle.tar.gz"
            res_dl = self._run_play(
                ["--bundle", url, "--bundle-sha256", bundle_sha, "--build-only"],
                extra_env={"AOE2_BUNDLE_TOKEN": "secret-tok"},
            )
            self.assertEqual(res_dl.returncode, 0, res_dl.stderr + res_dl.stdout)
            self.assertEqual(state["count"], 1)
            self.assertEqual(state["auths"], ["Bearer secret-tok"])
            self.assertTrue(os.path.isfile(os.path.join(self.repo_dir, "build", "web", "aoe2.wasm.gz")))
            self.assertTrue(os.path.isfile(os.path.join(self.repo_dir, "install", "age2_x1", "age2_x1.exe")))

            with open(os.path.join(self.repo_dir, ".env"), "r", encoding="utf-8") as f:
                dotenv = f.read()
            self.assertIn(f"AOE2_BUNDLE_URL={url}", dotenv)
            self.assertIn(f"AOE2_BUNDLE_SHA256={bundle_sha}", dotenv)
            self.assertIn("AOE2_INSTALL_DIR=./install", dotenv)
            self.assertNotIn("secret-tok", dotenv)

            # Re-running without --refresh-bundle must NOT re-download
            res_cached = self._run_play(["--build-only"])
            self.assertEqual(res_cached.returncode, 0, res_cached.stderr + res_cached.stdout)
            self.assertEqual(state["count"], 1)

            # Running with --refresh-bundle MUST re-download
            res_refresh = self._run_play(
                ["--refresh-bundle", "--build-only"],
                extra_env={"AOE2_BUNDLE_TOKEN": "secret-tok"},
            )
            self.assertEqual(res_refresh.returncode, 0, res_refresh.stderr + res_refresh.stdout)
            self.assertEqual(state["count"], 2)

            # Local file bundle works without network
            res_local = self._run_play(["--bundle", bundle_file, "--build-only"])
            self.assertEqual(res_local.returncode, 0, res_local.stderr + res_local.stdout)
            self.assertEqual(state["count"], 2)
        finally:
            srv.shutdown()
            srv.server_close()

        # Quick sanity check that tree exe matches unpacked exe
        with open(os.path.join(self.repo_dir, "install", "age2_x1", "age2_x1.exe"), "rb") as f:
            self.assertEqual(f.read(), tree["exe_bytes"])


if __name__ == "__main__":
    unittest.main()
