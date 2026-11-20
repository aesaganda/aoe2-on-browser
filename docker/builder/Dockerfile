# The whole toolchain for turning the owner's age2_x1.exe into the browser build, in
# one image: Python lifter deps, Ghidra (headless), emsdk, and the install tools
# (7z, cabextract, ffmpeg). It holds tools only: no game files and nothing derived
# from them (ADR-0002). The repository and the owner's files are mounted at run time;
# ./play.sh drives it.
#
#   docker build -f docker/builder/Dockerfile -t aoe2-recomp-builder .
#   docker run --rm -v "$PWD:/src" aoe2-recomp-builder make test
FROM ubuntu:24.04

ARG GHIDRA_URL=https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_12.2_build/ghidra_12.2_PUBLIC_20261016.zip
ARG EMSDK_VERSION=7.0.0

LABEL org.opencontainers.image.source="https://github.com/aesaganda/aoe2-on-browser" \
      org.opencontainers.image.title="aoe2-recomp-builder" \
      org.opencontainers.image.description="Toolchain (Ghidra, emsdk, lifter) for building the AoE2 recomp from your own copy of the game (no game data inside)"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    TOOLS_HOME=/opt/aoe2-recomp \
    AOE2R_TOOLS=/opt/aoe2-recomp \
    EMSDK=/opt/emsdk \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates curl git unzip patch pkg-config xz-utils bzip2 file \
      python3 python3-venv \
      openjdk-25-jdk-headless \
      mingw-w64 \
      p7zip-full cabextract ffmpeg \
      fonts-liberation \
      libsdl2-dev libsdl2-ttf-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Python lifter / test dependencies, where the Makefile looks for them ($(TOOLS_HOME)/venv)
COPY tools/requirements.txt /tmp/requirements.txt
RUN python3 -m venv "$TOOLS_HOME/venv" \
    && "$TOOLS_HOME/venv/bin/pip" install --no-cache-dir -q --upgrade pip \
    && "$TOOLS_HOME/venv/bin/pip" install --no-cache-dir -q -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Ghidra, where tools/ghidra/run.sh looks for it ($AOE2R_TOOLS/ghidra_*_PUBLIC). The
# release ships the decompiler prebuilt for linux_x86_64 only; on another CPU (linux
# arm64: Raspberry Pi, ARM servers) build it from the C++ source the release includes.
RUN curl -fsSL --retry 3 -o /tmp/ghidra.zip "$GHIDRA_URL" \
    && unzip -q /tmp/ghidra.zip -d "$TOOLS_HOME" \
    && rm /tmp/ghidra.zip \
    && g=$(ls -d "$TOOLS_HOME"/ghidra_*_PUBLIC) \
    && rm -rf "$g/docs" \
    && case "$(uname -m)" in x86_64) os=linux_x86_64 ;; aarch64) os=linux_arm_64 ;; *) os="linux_$(uname -m)" ;; esac \
    && d="$g/Ghidra/Features/Decompiler" \
    && if [ ! -x "$d/os/$os/decompile" ]; then \
         apt-get update && apt-get install -y --no-install-recommends bison flex \
         && cd "$d/src/decompile/cpp" && mkdir -p ghi_opt sla_opt \
         && make -j"$(nproc)" ARCH_TYPE= ghidra_opt sleigh_opt \
         && mkdir -p "$d/os/$os" && cp ghidra_opt "$d/os/$os/decompile" && cp sleigh_opt "$d/os/$os/sleigh" \
         && rm -rf ghi_opt sla_opt ghidra_opt sleigh_opt \
         && apt-get purge -y bison flex && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*; \
       fi \
    && test -x "$d/os/$os/decompile"

# emsdk, pinned. Then build the Emscripten ports and system libraries the game links
# (SDL2, SDL2_ttf, zlib, pthreads, WASMFS) once, so each game build doesn't.
RUN git clone --depth 1 https://github.com/emscripten-core/emsdk.git "$EMSDK" \
    && cd "$EMSDK" && ./emsdk install "$EMSDK_VERSION" && ./emsdk activate "$EMSDK_VERSION" \
    && rm -rf "$EMSDK/downloads" "$EMSDK/.git" \
    && . "$EMSDK/emsdk_env.sh" >/dev/null 2>&1 \
    && cd /tmp && printf 'int main(void){return 0;}\n' > warm.c \
    && emcc warm.c -o warm.js -O2 -mtail-call -pthread -sPROXY_TO_PTHREAD -sWASMFS \
         -sUSE_SDL=2 -sUSE_SDL_TTF=2 -sUSE_ZLIB=1 -sMODULARIZE -sENVIRONMENT=web,worker \
    && rm -f warm.* \
    && chmod -R a+rwX "$EMSDK/upstream/emscripten/cache"

COPY docker/builder/entrypoint.sh /usr/local/bin/aoe2-env
RUN chmod 755 /usr/local/bin/aoe2-env \
    && git config --system --add safe.directory '*'

WORKDIR /src
ENTRYPOINT ["/usr/local/bin/aoe2-env"]
CMD ["bash"]
