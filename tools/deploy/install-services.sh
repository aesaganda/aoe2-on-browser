#!/usr/bin/env bash
# Run the multiplayer platform as services (Phase 3, docs/phase3-multiplayer.md):
#
#   sudo tools/deploy/install-services.sh <install-root>     # install, enable, start
#   sudo tools/deploy/install-services.sh --remove           # stop, disable, remove
#
#   aoe2-relay.service  tools/net/relay.py      ws 127.0.0.1:8643/relay, tcp 127.0.0.1:8644
#   aoe2-web.service    tools/web/serve.py      the page on 127.0.0.1:8642; with the
#                                               install root, "Copy from this server" for
#                                               browsers on this machine only (serve.py)
#
# Both listen on loopback only; a proxy in front (tailscale serve) puts them on the
# tailnet. They run from this checkout, as the user who owns it, and restart if they
# stop. Under WSL this needs systemd ([boot] systemd=true in /etc/wsl.conf), and the
# distro has to be running: tools/deploy/wsl-keepalive.ps1 starts it at Windows sign-in.
set -euo pipefail
UNITS=(aoe2-relay aoe2-web)
DIR=/etc/systemd/system

if [[ "${1:-}" == --remove ]]; then
  for u in "${UNITS[@]}"; do systemctl disable --now "$u.service" 2>/dev/null || true; rm -f "$DIR/$u.service"; done
  systemctl daemon-reload
  echo "removed: ${UNITS[*]}"
  exit 0
fi

INSTALL=${1:?usage: install-services.sh <install-root> | --remove}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
OWNER=$(stat -c %U "$REPO")
[[ -d "$INSTALL/age2_x1" ]] || { echo "$INSTALL has no age2_x1/: give the game's install root" >&2; exit 1; }
[[ -f "$REPO/build/web/aoe2.wasm" ]] || echo "note: build/web/aoe2.wasm is missing; run make web before playing" >&2
python3 -c 'import websockets' 2>/dev/null || { echo "python3 needs websockets (pip install websockets)" >&2; exit 1; }

unit() {   # name, description, command
  cat > "$DIR/$1.service" <<EOF
[Unit]
Description=$2
After=network.target

[Service]
User=$OWNER
WorkingDirectory=$REPO
ExecStart=$3
Restart=always
RestartSec=2
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
}
unit aoe2-relay "aoe2-on-browser: multiplayer relay" "/usr/bin/python3 $REPO/tools/net/relay.py"
unit aoe2-web "aoe2-on-browser: web page" "/usr/bin/python3 $REPO/tools/web/serve.py --install $INSTALL"
systemctl daemon-reload
for u in "${UNITS[@]}"; do systemctl enable "$u.service" >/dev/null 2>&1; systemctl restart "$u.service"; done
sleep 2
systemctl --no-pager --lines=0 status "${UNITS[@]/%/.service}" | grep -E '^●|Active:'
