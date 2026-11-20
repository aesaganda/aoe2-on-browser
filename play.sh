#!/usr/bin/env bash
# One command from a fresh clone to the game in your browser. Needs only Docker.
#
#   First time, from your own two discs and your unprotected age2_x1.exe:
#     ./play.sh --exe age2_x1.exe --aok AoE2.iso --aoc AoE2_Conquerors.iso
#   or from an install tree you already have (age2_x1/age2_x1.exe, data/, sound/, ...):
#     ./play.sh --install ~/aoe2-install
#   After that, to start it again:      ./play.sh
#   To stop it:                         ./play.sh --stop
#
# It (1) builds the browser version of the game from YOUR age2_x1.exe inside the
# builder image (Ghidra + pcrecomp + emsdk), (2) lays out the install tree from your
# discs, and (3) starts the web server, the multiplayer relay and the gateway with
# Docker Compose on http://localhost:8642. Nothing of the game is downloaded from, or
# uploaded to, anywhere: it all stays in build/, orig/ and install/ (ADR-0002).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

usage() {
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --exe FILE        your unprotected age2_x1.exe (copied to orig/age2_x1.exe)
  --aok ISO         Age of Empires II disc image   } lay out install/ from your discs
  --aoc ISO         The Conquerors disc image      }
  --install DIR     use an install tree you already have instead of the discs
  --port N          port on this machine (default 8642, or AOE2_PORT in .env)
  --rebuild         rebuild the browser build even if one exists
  --build-only      build, but don't start the servers
  --stop            stop the servers
  -h, --help        this help

Environment:
  AOE2_BUILDER_IMAGE  builder image (default ghcr.io/aesaganda/aoe2-recomp-builder:latest;
                      built locally from docker/builder/Dockerfile when it can't be pulled)
  AOE2_JOBS           parallel compile jobs (default: all CPUs; lower it if RAM is short)
EOF
}

say()  { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
abspath() { (cd "$(dirname "$1")" && printf '%s/%s\n' "$(pwd)" "$(basename "$1")"); }

EXE="" AOK="" AOC="" INSTALL="" PORT="" REBUILD=0 BUILD_ONLY=0 STOP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --exe)        EXE="${2:?--exe needs a file}"; shift 2 ;;
    --aok)        AOK="${2:?--aok needs a file}"; shift 2 ;;
    --aoc)        AOC="${2:?--aoc needs a file}"; shift 2 ;;
    --install)    INSTALL="${2:?--install needs a directory}"; shift 2 ;;
    --port)       PORT="${2:?--port needs a number}"; shift 2 ;;
    --rebuild)    REBUILD=1; shift ;;
    --build-only) BUILD_ONLY=1; shift ;;
    --stop)       STOP=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *)            usage >&2; die "unknown option: $1" ;;
  esac
done

# --- prerequisites ---------------------------------------------------------------------
command -v docker >/dev/null || die "Docker is required: https://docs.docker.com/get-docker/"
docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon (is it running? are you in the docker group?)"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (the 'docker compose' plugin)"
compose() { docker compose --project-directory "$REPO" "$@"; }

if [[ $STOP == 1 ]]; then
  compose down
  exit 0
fi

# .env: created from .env.example on first run; play.sh only touches the keys it owns.
[[ -f .env ]] || cp .env.example .env
set_env() {  # set_env KEY VALUE
  local k="$1" v="$2"
  if grep -q "^$k=" .env; then
    awk -v k="$k" -v v="$v" 'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' .env > .env.tmp && mv .env.tmp .env
  else
    printf '%s=%s\n' "$k" "$v" >> .env
  fi
}
get_env() { sed -n "s/^$1=//p" .env | tail -1; }

# --- inputs ----------------------------------------------------------------------------
[[ -n "$AOK" || -n "$AOC" ]] && { [[ -n "$AOK" && -n "$AOC" ]] || die "--aok and --aoc go together (both disc images)"; }
[[ -n "$INSTALL" && -n "$AOK" ]] && die "use either --install or --aok/--aoc, not both"
for f in "$EXE" "$AOK" "$AOC"; do [[ -z "$f" || -f "$f" ]] || die "no such file: $f"; done
if [[ -n "$INSTALL" ]]; then
  [[ -d "$INSTALL" ]] || die "no such directory: $INSTALL"
  INSTALL="$(cd "$INSTALL" && pwd)"
  [[ -f "$INSTALL/age2_x1/age2_x1.exe" ]] || die "$INSTALL doesn't look like an install tree (no age2_x1/age2_x1.exe)"
fi

mkdir -p orig
if [[ -n "$EXE" ]]; then
  cmp -s "$EXE" orig/age2_x1.exe 2>/dev/null || { cp -f "$EXE" orig/age2_x1.exe; say "using $EXE as orig/age2_x1.exe"; }
elif [[ ! -f orig/age2_x1.exe && -n "$INSTALL" ]]; then
  cp -f "$INSTALL/age2_x1/age2_x1.exe" orig/age2_x1.exe
  say "using the age2_x1.exe from $INSTALL"
fi

have_build() { [[ -s build/web/aoe2.wasm && -s build/web/aoe2.js && -s build/web/aoe2.json ]]; }
exe_matches_build() {
  [[ -f orig/age2_x1.exe ]] || return 0   # nothing to compare with: keep the build there is
  local want
  if command -v sha256sum >/dev/null; then want=$(sha256sum orig/age2_x1.exe | cut -d' ' -f1)
  else want=$(shasum -a 256 orig/age2_x1.exe | cut -d' ' -f1); fi   # macOS
  grep -q "$want" build/web/aoe2.json
}
NEED_BUILD=0
if [[ $REBUILD == 1 ]] || ! have_build || ! exe_matches_build; then NEED_BUILD=1; fi
NEED_INSTALL=0
[[ -n "$AOK" ]] && NEED_INSTALL=1

if [[ $NEED_BUILD == 1 && ! -f orig/age2_x1.exe ]]; then
  usage >&2
  echo >&2
  die "no game to build from: pass --exe <your age2_x1.exe> (or --install <dir> that has one)"
fi

# --- builder ---------------------------------------------------------------------------
if [[ $NEED_BUILD == 1 || $NEED_INSTALL == 1 ]]; then
  # The lifter and the patches come from the submodules
  if [[ ! -d third_party/pcrecomp/runtime || ! -f third_party/pl_mpeg/pl_mpeg.h ]]; then
    [[ -d .git ]] || die "third_party/ is empty and this isn't a git checkout: git clone --recursive https://github.com/aesaganda/aoe2-on-browser"
    say "fetching submodules (pcrecomp, pl_mpeg)"
    git submodule update --init --recursive
  fi

  BUILDER="${AOE2_BUILDER_IMAGE:-ghcr.io/aesaganda/aoe2-recomp-builder:latest}"
  arch="$(uname -m)"
  if ! docker image inspect "$BUILDER" >/dev/null 2>&1; then
    # The published builder is linux/amd64 (Docker Desktop on Apple Silicon runs it
    # under Rosetta). On a Linux ARM host, build a native one instead.
    if [[ "$(uname -s)" == Linux && "$arch" != x86_64 ]] || ! { say "pulling $BUILDER"; docker pull "$BUILDER"; }; then
      BUILDER=aoe2-recomp-builder:local
      if ! docker image inspect "$BUILDER" >/dev/null 2>&1; then
        say "building the toolchain image locally (once; ~3 GB, takes a while)"
        net=(); [[ "$(uname -s)" == Linux ]] && net=(--network host)   # sidesteps build-time DNS trouble
        docker build ${net[@]+"${net[@]}"} -f docker/builder/Dockerfile -t "$BUILDER" .
      fi
    fi
  fi

  JOBS="${AOE2_JOBS:-}"
  run_builder() {  # run_builder [docker args...] -- command...
    local dargs=()
    while [[ $1 != -- ]]; do dargs+=("$1"); shift; done; shift
    local tty=(); [[ -t 1 ]] && tty=(-t)
    docker run --rm ${tty[@]+"${tty[@]}"} \
      -v "$REPO:/src" \
      -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
      -e GHIDRA_PROJ=/src/build/ghidra-proj \
      -e AOE2_JOBS="$JOBS" \
      ${dargs[@]+"${dargs[@]}"} "$BUILDER" "$@"
  }

  if [[ $NEED_INSTALL == 1 ]]; then
    AOK="$(abspath "$AOK")" AOC="$(abspath "$AOC")"
    [[ -f orig/age2_x1.exe ]] || die "the install needs your age2_x1.exe too: pass --exe"
    say "laying out install/ from your discs (unpacks the CABs, transcodes the cinematics)"
    rm -rf install.tmp
    run_builder -v "$AOK:/in/aok.iso:ro" -v "$AOC:/in/aoc.iso:ro" -e CHOWN_PATHS="/src/install.tmp" -- \
      bash tools/install_from_iso.sh /in/aok.iso /in/aoc.iso orig/age2_x1.exe install.tmp
    rm -rf install && mv install.tmp install
    INSTALL="$REPO/install"
  fi

  if [[ $NEED_BUILD == 1 ]]; then
    say "building the browser version from orig/age2_x1.exe (Ghidra analysis, lift, emcc: tens of minutes the first time)"
    run_builder -- bash -c '
      set -euo pipefail
      j=${AOE2_JOBS:-$(nproc)}
      make lift
      make -f tools/web/web.mk -j"$j" EXE=orig/age2_x1.exe
      gzip -9 -k -f build/web/aoe2.wasm
      ls -la build/web/aoe2.js build/web/aoe2.wasm build/web/aoe2.wasm.gz build/web/aoe2.json'
  fi
fi
have_build || die "build/web has no aoe2.wasm/aoe2.js/aoe2.json"

# --- install tree ----------------------------------------------------------------------
if [[ -z "$INSTALL" ]]; then
  cur="$(get_env AOE2_INSTALL_DIR)"
  if [[ -n "$cur" && -f "$cur/age2_x1/age2_x1.exe" ]]; then INSTALL="$cur"
  elif [[ -f install/age2_x1/age2_x1.exe ]]; then INSTALL="$REPO/install"
  else die "no install tree yet: pass --aok <AoE2.iso> --aoc <Conquerors.iso>, or --install <dir>"
  fi
fi
[[ "$INSTALL" == "$REPO/install" ]] && INSTALL=./install
set_env AOE2_INSTALL_DIR "$INSTALL"
set_env AOE2_BUILD_DIR ./build/web
set_env AOE2_WEB_DIR ./web
set_env AOE2_NO_INSTALL 0
[[ -n "$PORT" ]] && set_env AOE2_PORT "$PORT"

[[ $BUILD_ONLY == 1 ]] && { say "built; start it with ./play.sh"; exit 0; }

# --- servers ---------------------------------------------------------------------------
say "starting the web server, relay and gateway"
compose pull --quiet --ignore-pull-failures 2>/dev/null || true
compose up -d --wait

port="$(get_env AOE2_PORT)"; port="${port:-8642}"
bind="$(get_env AOE2_BIND)"; bind="${bind:-127.0.0.1}"
url="http://localhost:$port/"
cat <<EOF

  Ready: $url

  The first visit copies the game files into the browser's private storage
  (about 540 MB, once); later visits start straight away.
  Use "localhost" (a secure context): a plain http:// LAN address can't run the
  game. For other machines, put it behind HTTPS (docs/docker-nginx.md).
  Stop with: ./play.sh --stop
EOF
[[ "$bind" == 127.0.0.1 ]] || echo "  (listening on $bind:$port)"
