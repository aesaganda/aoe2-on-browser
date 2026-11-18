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

## 5. Updating After Rebuilds & Optional Native TCP Port

- **Updating frontend or WASM builds**: Run `make web` on the host; Nginx and `/api/version` pick up the updated `aoe2.wasm` / `web/` mount automatically. If `AOE2_INSTALL_DIR` contents change, restart the `web` service to refresh its tar bundle index:
  ```bash
  docker compose restart web
  ```
- **Exposing TCP port `8644` for native `build/native/aoe2` clients**: Uncomment the `ports:` section under `relay` in `docker-compose.yml` (`8644:8644`) and recreate the container (`docker compose up -d relay`).
