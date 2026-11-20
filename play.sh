#!/usr/bin/env bash
# One command from a fresh clone to the game in your browser. Needs only Docker.
#
#   Turnkey (no ISO, no build) — from a prebuilt bundle (URL, gh://, ssh://, or file):
#     ./play.sh --bundle https://example.com/aoe2-bundle.tar.gz
#   First time from your own discs and unprotected age2_x1.exe:
#     ./play.sh --exe age2_x1.exe --aok AoE2.iso --aoc AoE2_Conquerors.iso
#   Or from an install tree you already have (age2_x1/age2_x1.exe, data/, sound/, ...):
#     ./play.sh --install ~/aoe2-install
#   Pack a built host's build/web + install/ into one portable bundle:
#     ./play.sh --pack aoe2-bundle.tar.gz
#   After setup, to start it again:    ./play.sh
#   To stop it:                         ./play.sh --stop
#
# It either unpacks your prebuilt bundle, or (1) builds the browser version of the game
# from YOUR age2_x1.exe inside the builder image (Ghidra + pcrecomp + emsdk) and
# (2) lays out the install tree from your discs, then (3) starts the web server, the
# multiplayer relay and the gateway with Docker Compose on http://localhost:8642.
# Nothing of the game is committed to the repository (ADR-0002).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

usage() {
  sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --bundle SRC      use a prebuilt bundle (https://, http://, gh://owner/repo[@tag]/asset,
                    ssh://user@host/path, or local .tar.gz/.tar) — skips ISO & build
  --bundle-sha256 H verify the bundle archive's SHA-256 hex digest
  --refresh-bundle  re-download and re-unpack the configured bundle
  --pack [FILE]     pack build/web and install/ into a turnkey bundle (default: aoe2-bundle.tar.gz)
  --exe FILE        your unprotected age2_x1.exe (copied to orig/age2_x1.exe)
  --aok ISO         Age of Empires II disc image   } lay out install/ from your discs
  --aoc ISO         The Conquerors disc image      }
  --install DIR     use an install tree you already have instead of the discs
  --port N          port on this machine (default 8642, or AOE2_PORT in .env)
  --rebuild         rebuild the browser build even if one exists
  --build-only      prepare build/web and install/, but don't start the servers
  --stop            stop the servers
  -h, --help        this help

Environment:
  AOE2_BUNDLE_URL     default bundle source (persisted to .env when remote)
  AOE2_BUNDLE_SHA256  expected SHA-256 hex digest for the bundle archive
  AOE2_BUNDLE_TOKEN   optional Bearer token sent on HTTP(S) bundle downloads (never saved to .env)
  AOE2_BUNDLE_HEADER  optional extra HTTP header for bundle downloads (never saved to .env)
  AOE2_BUILDER_IMAGE  builder image (default ghcr.io/aesaganda/aoe2-recomp-builder:latest;
                      built locally from docker/builder/Dockerfile when it can't be pulled)
  AOE2_JOBS           parallel compile jobs (default: all CPUs; lower it if RAM is short)
EOF
}

say()  { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
abspath() { (cd "$(dirname "$1")" && printf '%s/%s\n' "$(pwd)" "$(basename "$1")"); }

EXE="" AOK="" AOC="" INSTALL="" PORT="" REBUILD=0 BUILD_ONLY=0 STOP=0
BUNDLE="" BUNDLE_CLI=0 BUNDLE_SHA256="" BUNDLE_SHA_CLI=0 REFRESH_BUNDLE=0
PACK=0 PACK_OUT="aoe2-bundle.tar.gz"
ENV_BUNDLE="${AOE2_BUNDLE_URL:-}"
ENV_BUNDLE_SHA256="${AOE2_BUNDLE_SHA256:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle)         BUNDLE="${2:?--bundle needs a URL or file}"; BUNDLE_CLI=1; shift 2 ;;
    --bundle-sha256)  BUNDLE_SHA256="${2:?--bundle-sha256 needs a digest}"; BUNDLE_SHA_CLI=1; shift 2 ;;
    --refresh-bundle) REFRESH_BUNDLE=1; shift ;;
    --pack)
      PACK=1
      if [[ $# -ge 2 && "$2" != -* ]]; then
        PACK_OUT="$2"
        shift 2
      else
        shift
      fi
      ;;
    --exe)            EXE="${2:?--exe needs a file}"; shift 2 ;;
    --aok)            AOK="${2:?--aok needs a file}"; shift 2 ;;
    --aoc)            AOC="${2:?--aoc needs a file}"; shift 2 ;;
    --install)        INSTALL="${2:?--install needs a directory}"; shift 2 ;;
    --port)           PORT="${2:?--port needs a number}"; shift 2 ;;
    --rebuild)        REBUILD=1; shift ;;
    --build-only)     BUILD_ONLY=1; shift ;;
    --stop)           STOP=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)                usage >&2; die "unknown option: $1" ;;
  esac
done

BUNDLE_TMP=""
cleanup() {
  if [[ -n "${BUNDLE_TMP:-}" && -d "$BUNDLE_TMP" ]]; then
    rm -rf "$BUNDLE_TMP"
  fi
}
trap cleanup EXIT

# --- prerequisites ---------------------------------------------------------------------
DOCKER_READY=0
require_docker() {
  [[ $DOCKER_READY == 1 ]] && return 0
  command -v docker >/dev/null || die "Docker is required: https://docs.docker.com/get-docker/"
  docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon (is it running? are you in the docker group?)"
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (the 'docker compose' plugin)"
  DOCKER_READY=1
}
compose() { require_docker; docker compose --project-directory "$REPO" "$@"; }

if [[ $STOP == 1 ]]; then
  compose down
  exit 0
fi

if [[ $PACK == 0 && $BUILD_ONLY == 0 ]]; then
  require_docker
fi

# .env: created from .env.example on first run; play.sh only touches the keys it owns.
[[ -f .env ]] || cp .env.example .env
set_env() {  # set_env KEY VALUE
  local k="$1" v="$2"
  if grep -q "^$k=" .env; then
    K="$k" V="$v" awk 'BEGIN{FS=OFS="="; k=ENVIRON["K"]; v=ENVIRON["V"]} $1==k {print k "=" v; next} {print}' .env > .env.tmp && mv .env.tmp .env
  else
    printf '%s=%s\n' "$k" "$v" >> .env
  fi
}
get_env() { sed -n "s/^$1=//p" .env | tail -1; }

run_bundle_py() {
  if command -v python3 >/dev/null 2>&1; then
    python3 "$REPO/tools/bundle.py" "$@"
    return $?
  fi

  require_docker
  local cmd="${1:-}"
  shift || true
  local dargs=()
  local pargs=("$cmd")

  if [[ "$cmd" == "unpack" && $# -ge 1 ]]; then
    local arc_abs
    arc_abs="$(abspath "$1")"
    shift
    dargs+=(-v "$arc_abs:/in/bundle:ro")
    pargs+=("/in/bundle")
  fi

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --build-dir|--install-dir)
        local flag="$1" val="$2"
        shift 2
        local abs_val
        abs_val="$(abspath "$val")"
        case "$abs_val" in
          "$REPO"/*)
            pargs+=("$flag" "/src/${abs_val#"$REPO"/}")
            ;;
          "$REPO")
            pargs+=("$flag" "/src")
            ;;
          *)
            if [[ "$cmd" == "pack" ]]; then
              local mnt="/in/$(basename "$flag")"
              dargs+=(-v "$abs_val:$mnt:ro")
              pargs+=("$flag" "$mnt")
            else
              die "without host python3, $flag must stay inside $REPO (got $val)"
            fi
            ;;
        esac
        ;;
      --out)
        local val="$2"
        shift 2
        mkdir -p "$(dirname "$val")"
        local abs_val
        abs_val="$(abspath "$val")"
        case "$abs_val" in
          "$REPO"/*)
            pargs+=(--out "/src/${abs_val#"$REPO"/}")
            ;;
          *)
            local out_dir out_base
            out_dir="$(dirname "$abs_val")"
            out_base="$(basename "$abs_val")"
            mkdir -p "$out_dir"
            dargs+=(-v "$out_dir:/out")
            pargs+=(--out "/out/$out_base")
            ;;
        esac
        ;;
      *)
        pargs+=("$1")
        shift
        ;;
    esac
  done

  docker run --rm -u "$(id -u):$(id -g)" -v "$REPO:/src" -w /src \
    ${dargs[@]+"${dargs[@]}"} python:3.12-slim python3 /src/tools/bundle.py "${pargs[@]}"
}

# --- --pack mode -----------------------------------------------------------------------
if [[ $PACK == 1 ]]; then
  [[ $BUNDLE_CLI == 1 ]] && die "--pack cannot be combined with --bundle"
  [[ -n "$AOK" || -n "$AOC" ]] && die "--pack cannot be combined with --aok/--aoc"
  [[ -n "$EXE" ]] && die "--pack cannot be combined with --exe"
  [[ $STOP == 1 ]] && die "--pack cannot be combined with --stop"
  [[ $REFRESH_BUNDLE == 1 ]] && die "--pack cannot be combined with --refresh-bundle"
  [[ $REBUILD == 1 ]] && die "--pack cannot be combined with --rebuild"

  cur_build="$(get_env AOE2_BUILD_DIR)"
  PACK_BUILD_DIR="${cur_build:-./build/web}"

  if [[ -n "$INSTALL" ]]; then
    [[ -d "$INSTALL" ]] || die "no such directory: $INSTALL"
  else
    cur_inst="$(get_env AOE2_INSTALL_DIR)"
    if [[ -n "$cur_inst" && -f "$cur_inst/age2_x1/age2_x1.exe" ]]; then
      INSTALL="$cur_inst"
    elif [[ -f "$REPO/install/age2_x1/age2_x1.exe" ]]; then
      INSTALL="$REPO/install"
    elif [[ -f "$REPO/../aoe2-recomp-work/install/age2_x1/age2_x1.exe" ]]; then
      INSTALL="$REPO/../aoe2-recomp-work/install"
    else
      die "no install tree found to pack (checked AOE2_INSTALL_DIR, ./install, ../aoe2-recomp-work/install; pass --install DIR)"
    fi
  fi

  say "packing turnkey bundle -> $PACK_OUT"
  run_bundle_py pack --build-dir "$PACK_BUILD_DIR" --install-dir "$INSTALL" --out "$PACK_OUT"
  exit 0
fi

# --- inputs ----------------------------------------------------------------------------
if [[ $BUNDLE_CLI == 1 && ( -n "$EXE" || -n "$AOK" || -n "$AOC" || -n "$INSTALL" || $REBUILD == 1 ) ]]; then
  die "--bundle cannot be combined with --exe, --aok/--aoc, --install, or --rebuild"
fi

if [[ $BUNDLE_CLI == 0 ]]; then
  if [[ -n "$EXE" || -n "$AOK" || -n "$AOC" || -n "$INSTALL" || $REBUILD == 1 ]]; then
    BUNDLE=""
  elif [[ -n "$ENV_BUNDLE" ]]; then
    BUNDLE="$ENV_BUNDLE"
  else
    BUNDLE="$(get_env AOE2_BUNDLE_URL)"
  fi
fi

if [[ $BUNDLE_SHA_CLI == 0 ]]; then
  if [[ -n "$ENV_BUNDLE_SHA256" ]]; then
    BUNDLE_SHA256="$ENV_BUNDLE_SHA256"
  elif [[ $BUNDLE_CLI == 0 ]]; then
    BUNDLE_SHA256="$(get_env AOE2_BUNDLE_SHA256)"
  fi
fi

[[ -n "$AOK" || -n "$AOC" ]] && { [[ -n "$AOK" && -n "$AOC" ]] || die "--aok and --aoc go together (both disc images)"; }
[[ -n "$INSTALL" && -n "$AOK" ]] && die "use either --install or --aok/--aoc, not both"
for f in "$EXE" "$AOK" "$AOC"; do [[ -z "$f" || -f "$f" ]] || die "no such file: $f"; done
if [[ -n "$INSTALL" ]]; then
  [[ -d "$INSTALL" ]] || die "no such directory: $INSTALL"
  INSTALL="$(cd "$INSTALL" && pwd)"
  [[ -f "$INSTALL/age2_x1/age2_x1.exe" ]] || die "$INSTALL doesn't look like an install tree (no age2_x1/age2_x1.exe)"
fi

have_build() { [[ -s build/web/aoe2.wasm && -s build/web/aoe2.js && -s build/web/aoe2.json ]]; }
have_install() {
  local cur
  cur="$(get_env AOE2_INSTALL_DIR)"
  { [[ -n "$cur" && -f "$cur/age2_x1/age2_x1.exe" ]]; } || [[ -f "$REPO/install/age2_x1/age2_x1.exe" ]]
}
exe_matches_build() {
  [[ -f build/web/.bundle.json && -z "$EXE" ]] && return 0
  [[ -f orig/age2_x1.exe ]] || return 0   # nothing to compare with: keep the build there is
  local want
  if command -v sha256sum >/dev/null; then want=$(sha256sum orig/age2_x1.exe | cut -d' ' -f1)
  else want=$(shasum -a 256 orig/age2_x1.exe | cut -d' ' -f1); fi   # macOS
  grep -q "$want" build/web/aoe2.json
}

NEED_BUNDLE=0
BUNDLE_USED=0
if [[ -n "$BUNDLE" ]]; then
  if [[ $BUNDLE_CLI == 1 || $REFRESH_BUNDLE == 1 ]] || ! have_build || ! have_install; then
    NEED_BUNDLE=1
  fi
elif [[ $REFRESH_BUNDLE == 1 ]]; then
  die "--refresh-bundle requires --bundle <URL|file> or AOE2_BUNDLE_URL in .env"
fi

FETCHED_ARCHIVE=""
fetch_bundle() {
  local src="$1" dest="$2"
  if [[ -f "$src" ]]; then
    FETCHED_ARCHIVE="$(abspath "$src")"
    return 0
  fi

  case "$src" in
    gh://*)
      command -v gh >/dev/null || die "GitHub CLI (gh) is required for gh:// bundle URLs: https://cli.github.com/"
      local rest="${src#gh://}"
      local owner="${rest%%/*}"
      local after_owner="${rest#*/}"
      [[ -n "$owner" && "$after_owner" != "$rest" ]] || die "invalid gh:// URL (expected gh://owner/repo[@tag]/asset): $src"
      local repo_tag="${after_owner%%/*}"
      local asset="${after_owner#*/}"
      [[ -n "$repo_tag" && -n "$asset" && "$asset" != "$after_owner" ]] || die "invalid gh:// URL (expected gh://owner/repo[@tag]/asset): $src"
      local repo_name="$repo_tag" tag=""
      if [[ "$repo_tag" == *"@"* ]]; then
        repo_name="${repo_tag%%@*}"
        tag="${repo_tag#*@}"
        [[ -n "$repo_name" && -n "$tag" ]] || die "invalid gh:// URL tag: $src"
      fi
      local gh_args=(-R "$owner/$repo_name" -p "$asset" -D "$BUNDLE_TMP")
      say "downloading bundle $asset from GitHub release ($owner/$repo_name${tag:+@$tag})"
      if [[ -n "$tag" ]]; then
        gh release download "$tag" "${gh_args[@]}"
      else
        gh release download "${gh_args[@]}"
      fi
      FETCHED_ARCHIVE="$BUNDLE_TMP/$asset"
      if [[ ! -f "$FETCHED_ARCHIVE" ]]; then
        FETCHED_ARCHIVE="$(find "$BUNDLE_TMP" -maxdepth 1 -type f | head -n 1)"
      fi
      [[ -f "$FETCHED_ARCHIVE" ]] || die "gh release download did not produce $asset"
      ;;
    ssh://*|scp://*)
      command -v scp >/dev/null || die "scp (OpenSSH) is required for ssh:// / scp:// bundle URLs"
      local rest="${src#ssh://}"
      rest="${rest#scp://}"
      local hostport="${rest%%/*}"
      local rpath="/${rest#*/}"
      [[ -n "$hostport" && "$rest" == */* && "$rpath" != "/" ]] || die "invalid ssh:// URL (expected ssh://[user@]host[:port]/path): $src"
      case "$rpath" in
        "/~/"*|"/~") rpath="${rpath#/}" ;;
      esac
      local scp_args=() target="$hostport"
      case "$hostport" in
        *:[0-9]*)
          local port_part="${hostport##*:}"
          target="${hostport%:*}"
          scp_args+=(-P "$port_part")
          ;;
      esac
      say "copying bundle from $target:$rpath"
      scp ${scp_args[@]+"${scp_args[@]}"} "$target:$rpath" "$dest" || die "scp failed for $src"
      FETCHED_ARCHIVE="$dest"
      ;;
    http://*|https://*)
      local hdrs=()
      if [[ -n "${AOE2_BUNDLE_TOKEN:-}" ]]; then
        hdrs+=(-H "Authorization: Bearer ${AOE2_BUNDLE_TOKEN}")
      fi
      if [[ -n "${AOE2_BUNDLE_HEADER:-}" ]]; then
        hdrs+=(-H "${AOE2_BUNDLE_HEADER}")
      fi
      if [[ "$src" == https://api.github.com/repos/*/releases/assets/* ]]; then
        case "${AOE2_BUNDLE_HEADER:-}" in
          [Aa][Cc][Cc][Ee][Pp][Tt]:*|*[Aa][Cc][Cc][Ee][Pp][Tt]:*) ;;
          *) hdrs+=(-H "Accept: application/octet-stream") ;;
        esac
      fi
      say "downloading bundle from $src"
      if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 --retry-delay 2 --progress-bar ${hdrs[@]+"${hdrs[@]}"} -o "$dest" "$src" \
          || die "failed to download bundle from $src"
      elif command -v wget >/dev/null 2>&1; then
        local whdrs=()
        if [[ -n "${AOE2_BUNDLE_TOKEN:-}" ]]; then
          whdrs+=(--header="Authorization: Bearer ${AOE2_BUNDLE_TOKEN}")
        fi
        if [[ -n "${AOE2_BUNDLE_HEADER:-}" ]]; then
          whdrs+=(--header="${AOE2_BUNDLE_HEADER}")
        fi
        if [[ "$src" == https://api.github.com/repos/*/releases/assets/* ]]; then
          case "${AOE2_BUNDLE_HEADER:-}" in
            [Aa][Cc][Cc][Ee][Pp][Tt]:*|*[Aa][Cc][Cc][Ee][Pp][Tt]:*) ;;
            *) whdrs+=(--header="Accept: application/octet-stream") ;;
          esac
        fi
        wget -O "$dest" ${whdrs[@]+"${whdrs[@]}"} "$src" || die "failed to download bundle from $src"
      else
        die "curl or wget is required to download HTTP(S) bundles"
      fi
      FETCHED_ARCHIVE="$dest"
      ;;
    *)
      die "bundle not found or unsupported source: $src (use a local .tar.gz/.tar file, https://, http://, gh://owner/repo[@tag]/asset, or ssh://user@host/path)"
      ;;
  esac
}

if [[ $NEED_BUNDLE == 1 ]]; then
  BUNDLE_TMP="$(mktemp -d "${TMPDIR:-/tmp}/aoe2-bundle.XXXXXX")"
  fetch_bundle "$BUNDLE" "$BUNDLE_TMP/aoe2-bundle.tar.gz"
  say "verifying and unpacking bundle"
  unpack_args=(unpack "$FETCHED_ARCHIVE" --build-dir "$REPO/build/web" --install-dir "$REPO/install" --source "$BUNDLE")
  if [[ -n "$BUNDLE_SHA256" ]]; then
    unpack_args+=(--sha256 "$BUNDLE_SHA256")
  fi
  run_bundle_py "${unpack_args[@]}"
  INSTALL="$REPO/install"
  BUNDLE_USED=1
  NEED_BUILD=0
  NEED_INSTALL=0
  case "$BUNDLE" in
    http://*|https://*|gh://*|ssh://*|scp://*)
      set_env AOE2_BUNDLE_URL "$BUNDLE"
      set_env AOE2_BUNDLE_SHA256 "$BUNDLE_SHA256"
      ;;
  esac
else
  mkdir -p orig
  if [[ -n "$EXE" ]]; then
    cmp -s "$EXE" orig/age2_x1.exe 2>/dev/null || { cp -f "$EXE" orig/age2_x1.exe; say "using $EXE as orig/age2_x1.exe"; }
  elif [[ ! -f orig/age2_x1.exe && -n "$INSTALL" ]]; then
    cp -f "$INSTALL/age2_x1/age2_x1.exe" orig/age2_x1.exe
    say "using the age2_x1.exe from $INSTALL"
  fi

  NEED_BUILD=0
  if [[ $REBUILD == 1 ]] || ! have_build || ! exe_matches_build; then NEED_BUILD=1; fi
  NEED_INSTALL=0
  [[ -n "$AOK" ]] && NEED_INSTALL=1

  if [[ $NEED_BUILD == 1 && ! -f orig/age2_x1.exe ]]; then
    usage >&2
    echo >&2
    die "no game build yet: pass --bundle <URL|file> (or set AOE2_BUNDLE_URL), or build from your copy with --exe <age2_x1.exe> --aok <AoE2.iso> --aoc <AoE2_Conquerors.iso>"
  fi
fi

# --- builder ---------------------------------------------------------------------------
if [[ $NEED_BUILD == 1 || $NEED_INSTALL == 1 ]]; then
  require_docker

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
    require_docker
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
    rm -f build/web/.bundle.json
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
  else die "no install tree yet: pass --bundle <URL|file>, --aok <AoE2.iso> --aoc <Conquerors.iso>, or --install <dir>"
  fi
fi
[[ "$INSTALL" == "$REPO/install" ]] && INSTALL=./install
set_env AOE2_INSTALL_DIR "$INSTALL"
set_env AOE2_BUILD_DIR ./build/web
set_env AOE2_WEB_DIR ./web
set_env AOE2_NO_INSTALL 0
[[ -n "$PORT" ]] && set_env AOE2_PORT "$PORT"

if [[ $BUILD_ONLY == 1 ]]; then
  if [[ $BUNDLE_USED == 1 ]]; then
    say "bundle ready; start it with ./play.sh"
  else
    say "built; start it with ./play.sh"
  fi
  exit 0
fi

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
