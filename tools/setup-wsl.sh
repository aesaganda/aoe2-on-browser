#!/usr/bin/env bash
# One-time toolchain setup for Ubuntu 24.04 (native or WSL).
#   - Python venv with pefile / LIEF / unicorn / capstone / pytest
#   - Ghidra (official NSA GitHub release) for headless analysis
# System packages (needs root):  build-essential python3-venv openjdk-21-jdk-headless unzip curl xvfb
#   native build and install:   libsdl2-dev libsdl2-ttf-dev zlib1g-dev p7zip-full cabextract ffmpeg (ADR-0004)
#   determinism oracle:         gdb; wine wine32:i386 (dpkg --add-architecture i386) for the original
set -euo pipefail

TOOLS_HOME="${AOE2R_TOOLS:-$HOME/.local/share/aoe2-recomp}"
VENV="$TOOLS_HOME/venv"
GHIDRA_VER="${GHIDRA_VER:-latest}"

mkdir -p "$TOOLS_HOME"

if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$(dirname "$0")/requirements.txt"
"$VENV/bin/python" - <<'EOF'
import pefile, lief, unicorn, capstone
print("python libs:", "pefile", pefile.__version__, "| lief", lief.__version__,
      "| unicorn", unicorn.__version__, "| capstone", capstone.__version__)
EOF

if ! ls -d "$TOOLS_HOME"/ghidra_*_PUBLIC >/dev/null 2>&1; then
  api="https://api.github.com/repos/NationalSecurityAgency/ghidra/releases/$GHIDRA_VER"
  [[ "$GHIDRA_VER" != latest ]] && api="https://api.github.com/repos/NationalSecurityAgency/ghidra/releases/tags/Ghidra_${GHIDRA_VER}_build"
  url=$(curl -fsSL "$api" | grep -o '"browser_download_url": *"[^"]*\.zip"' | head -1 | cut -d'"' -f4)
  echo "downloading $url"
  curl -fL --retry 3 -o "$TOOLS_HOME/ghidra.zip" "$url"
  unzip -q "$TOOLS_HOME/ghidra.zip" -d "$TOOLS_HOME"
  rm "$TOOLS_HOME/ghidra.zip"
fi
GHIDRA_DIR=$(ls -d "$TOOLS_HOME"/ghidra_*_PUBLIC | tail -1)
echo "ghidra: $GHIDRA_DIR"
java -version 2>&1 | head -1
