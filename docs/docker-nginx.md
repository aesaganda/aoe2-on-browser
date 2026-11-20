# Docker & Nginx Reverse Proxy Deployment

This guide explains how to run **Age of Empires II: The Conquerors (WebAssembly Edition)** in Docker behind a host **Nginx** TLS reverse proxy.

## Architecture

```text
Browser (HTTPS / WSS)
  │
  ▼
Host Nginx (:80 / :443, TLS + HSTS)  ── deploy/nginx/aoe2-reverse-proxy.conf
  │  http://127.0.0.1:8642
  ▼
Docker `gateway` (nginx:1.27-alpine)  ── deploy/nginx/gateway.conf
  ├── /, /aoe2.*       ──► static files + gzip_static (aoe2.wasm.gz)
  ├── /relay, /relay/* ──► `relay` container (:8643 WebSocket + HTTP lobby/chat)
  └── /api/*, /install/*, fallback ──► `web` container (:8642 tar streamer & API)
```

Copyrighted game assets (`build/web/`, `install/`) are never baked into the Docker image. They are bind-mounted read-only (`:ro`) at runtime.

---

## 1. Prerequisites

1. Build the WebAssembly artifacts once on your build host:
   ```bash
   source ~/emsdk/emsdk_env.sh
   make web
   ```
   Verify `build/web/aoe2.wasm`, `build/web/aoe2.js`, and `build/web/aoe2.json` exist.
2. Prepare the game install root directory (for example `../aoe2-recomp-work/install` containing `age2_x1/`, `data/`, `sound/`, etc.).

---

## 2. Start the Docker Stack

1. Create `.env` from `.env.example`:
   ```bash
   cp .env.example .env
   ```
   Edit `AOE2_INSTALL_DIR` (and `AOE2_VERSION` if desired, e.g. `AOE2_VERSION=$(git describe --always)`).

   > Note: If you run Docker Compose after previously running the WSL systemd services (`aoe2-web`, `aoe2-relay`) on port `8642`, stop those services first (`sudo systemctl stop aoe2-web aoe2-relay`) or set a different `AOE2_PORT` in `.env`.

2. Build and start the containers:
   ```bash
   docker compose up -d --build
   docker compose ps
   ```

3. Verify container health locally on loopback:
   ```bash
   curl -i http://127.0.0.1:8642/health
   curl -i http://127.0.0.1:8642/health/deep
   ```
   Both should return `HTTP/1.1 200 OK`, including `Cross-Origin-Opener-Policy: same-origin` and `Cross-Origin-Embedder-Policy: require-corp`.

---

## 3. Configure Host Nginx + TLS

1. Copy `deploy/nginx/aoe2-reverse-proxy.conf` to Nginx:
   ```bash
   sudo cp deploy/nginx/aoe2-reverse-proxy.conf /etc/nginx/sites-available/aoe2.conf
   ```
2. Edit `/etc/nginx/sites-available/aoe2.conf`:
   - Replace `aoe2.example.com` with your domain.
   - Update `ssl_certificate` / `ssl_certificate_key` (or obtain certificates with Certbot: `sudo certbot --nginx -d aoe2.example.com`).
3. Enable and reload Nginx:
   ```bash
   sudo ln -s /etc/nginx/sites-available/aoe2.conf /etc/nginx/sites-enabled/aoe2.conf
   sudo nginx -t && sudo systemctl reload nginx
   ```

---

## 4. Production Verification Checklist

1. **Cross-Origin Isolation (`SharedArrayBuffer`)**:
   - Inspect response headers on `https://aoe2.example.com/`: confirm `Cross-Origin-Opener-Policy: same-origin`, `Cross-Origin-Embedder-Policy: require-corp`, and `Strict-Transport-Security` appear once (not duplicated).
2. **Precompressed WASM delivery**:
   - Confirm `aoe2.wasm?v=...` returns `Content-Type: application/wasm` and `Content-Encoding: gzip` (~9.2 MB wire size vs 42 MB raw).
3. **Streaming bundle**:
   - Confirm `https://aoe2.example.com/install/bundle.tar` starts streaming bytes immediately without 504 timeouts (`proxy_buffering off` and `X-Accel-Buffering: no`).
4. **WebSocket DirectPlay relay**:
   - Open the in-game `Online` modal -> click `Host a game` -> verify WebSocket upgrades (`101 Switching Protocols`) on `wss://aoe2.example.com/relay`, the room appears in the server browser, and lobby chat works.

---

## 4b. Alternative: Nginx Proxy Manager on another LAN host (e.g. Raspberry Pi)

Use this instead of section 3 when TLS is terminated by a dockerized Nginx Proxy Manager (NPM) on a different machine and the stack runs in Docker Engine inside WSL2 on Windows.

```text
Internet ──443──► Pi: NPM (TLS, HSTS) ──http──► Windows <LAN IP>:8642
                                                  │ netsh portproxy
                                                  ▼
                                    127.0.0.1:8642 (WSL localhost forwarding)
                                                  ▼
                                    WSL Docker `gateway` (unchanged, AOE2_BIND=127.0.0.1)
```

1. **Windows side** (elevated PowerShell, from the repo's `deploy/windows/` folder). Give the Windows PC a DHCP reservation first so its LAN IP is stable:
   ```powershell
   .\expose-to-lan.ps1 -ListenAddress <windows-lan-ip> -ProxyHost <pi-lan-ip>
   ```
   This adds a port proxy `<windows-lan-ip>:8642 → 127.0.0.1:8642` and a firewall rule that only admits the Pi. Undo with `-Remove`.
2. **Check from the Pi:** `curl -i http://<windows-lan-ip>:8642/health` → `200` with `Cross-Origin-Embedder-Policy: require-corp`.
3. **NPM → Hosts → Proxy Hosts → Add:**
   - *Details*: domain `aoe2.example.com`, scheme `http`, forward hostname `<windows-lan-ip>`, port `8642`.
     **Websockets Support: ON**. **Cache Assets: OFF** (the gateway already sets `Cache-Control`). Block Common Exploits may stay on.
   - *SSL*: Let's Encrypt certificate, Force SSL, HTTP/2, HSTS.
   - *Advanced* (custom Nginx configuration — inherited by NPM's `location /`):
     ```nginx
     # long-lived DirectPlay WebSocket (/relay)
     proxy_read_timeout 3600s;
     proxy_send_timeout 3600s;
     # stream the ~585 MB /install/bundle.tar instead of spooling it to the Pi's SD card
     proxy_buffering off;
     proxy_request_buffering off;
     proxy_max_temp_file_size 0;
     ```
4. Run the checklist in section 4 against the public domain. COOP/COEP/CSP come from the gateway and pass through NPM unchanged; HSTS comes from NPM. Real client IPs survive the chain: NPM sends `X-Forwarded-For`/`X-Real-IP`, and the gateway trusts private-range hops.

Notes:
- The containers only run while the WSL distro is up. `vmIdleTimeout=-1` in `.wslconfig` keeps it alive once started, but after a Windows reboot something must start it (e.g. a scheduled task at startup running `wsl -d ubuntu-24.04 -u root -- true`).
- If the Pi is on the same Tailscale tailnet, you can point NPM at the PC's Tailscale IP instead and skip the DHCP reservation (use that IP as `-ListenAddress`).

---

## 5. Updating After Rebuilds & Optional Native TCP Port

- **Updating frontend or WASM builds**: Run `make web` on the host; Nginx and `/api/version` pick up the updated `aoe2.wasm` / `web/` mount automatically. If `AOE2_INSTALL_DIR` contents change, restart the `web` service to refresh its tar bundle index:
  ```bash
  docker compose restart web
  ```
- **Exposing TCP port `8644` for native `build/native/aoe2` clients**: Uncomment the `ports:` section under `relay` in `docker-compose.yml` (`8644:8644`) and recreate the container (`docker compose up -d relay`).

---

## 6. Running on a Raspberry Pi or Mac (multi-arch image on ghcr.io)

The `relay` and `web` services are plain Python (`websockets` only) and the game itself runs as WebAssembly in the player's browser, so the server side has no x86 dependency. `ghcr.io/aesaganda/aoe2-recomp-web` is published for `linux/amd64`, `linux/arm64` and `linux/arm/v7`; `nginx:1.27-alpine` is multi-arch too.

| Host | Platform pulled |
| --- | --- |
| Raspberry Pi 3/4/5, 64-bit Raspberry Pi OS / Ubuntu | `linux/arm64` |
| Raspberry Pi 2/3/4, 32-bit Raspberry Pi OS | `linux/arm/v7` |
| Mac with Apple Silicon (Docker Desktop, OrbStack, Colima) | `linux/arm64` |
| Intel Mac, x86 Linux | `linux/amd64` |

**What the image does not contain** (ADR-0002): `build/web/` (`aoe2.js`, `aoe2.wasm`) and the `install/` game data. Build them on the x86 Linux/WSL machine (`make web`) and copy them to the Pi/Mac; the `.wasm` is architecture-independent.

The turnkey way is to pack both into one verified archive on the build host and unpack + start with one command on the Pi/Mac:

```bash
# On the build host:
./play.sh --pack aoe2-bundle.tar.gz

# On the Pi/Mac (no submodules, ISO, or build needed):
git clone https://github.com/aesaganda/aoe2-on-browser.git && cd aoe2-on-browser
./play.sh --bundle ssh://user@build-host/path/to/aoe2-bundle.tar.gz --bundle-sha256 <SHA256>
```

Or copy the folders manually with `rsync`:

```bash
git clone https://github.com/aesaganda/aoe2-on-browser.git && cd aoe2-on-browser
cp .env.example .env                       # point AOE2_BUILD_DIR / AOE2_INSTALL_DIR at the copied folders
rsync -a <pc>:aoe2-recomp-backup/build/web/ ./build/web/
rsync -a <pc>:aoe2-recomp-work/install/ ../aoe2-recomp-work/install/
docker login ghcr.io                       # only while the package is private
docker compose pull
docker compose up -d --no-build
```

Notes:
- On a Pi, keep `install/` on an SSD rather than the SD card: `web` streams the ~585 MB `/install/bundle.tar` from it to each new player.
- On macOS, the bind-mounted folders must be inside a path Docker Desktop shares (by default `/Users`).
- `docker compose up -d --build` still works on any of these hosts; it builds natively for the host's architecture.

**Publishing a new image** (on the x86 build machine, once: `apt install docker-buildx qemu-user-static` and `docker login ghcr.io` with a PAT that has `write:packages`):

```bash
make image-push          # buildx: amd64 + arm64 + arm/v7, tags :latest and :sha-<commit>
```

Under WSL, systemd does not register QEMU at boot; if `docker buildx inspect aoe2-multi` lists no `linux/arm64`, run `sudo /usr/lib/systemd/systemd-binfmt /usr/lib/binfmt.d/qemu-*.conf` once per WSL start.
