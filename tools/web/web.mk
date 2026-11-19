# Browser build (Phase 2): the same lifted code, win32hle and host, compiled with
# Emscripten to WebAssembly. Invoked by the top-level `make web` (needs emsdk's emcc
# on PATH: source ~/emsdk/emsdk_env.sh).
#
#   build/web/aoe2.js + aoe2.wasm, served with web/index.html (tools/web/serve.py)
#
# Memory is flat (RECOMP_FLAT_MEMORY): a guest VA is an address in wasm memory. The
# PE image (0x400000 - 0x7A4000) lives below 16 MB, which win32hle keeps for fixed
# reservations (guest_mem.c, HLE_FIXED_END); the program's own data starts above it
# (GLOBAL_BASE). The game runs on a pthread (PROXY_TO_PTHREAD), so the page must be
# cross-origin isolated (serve.py sends the headers).
GEN     := build/lift/gen
OBJ     := build/web/obj
OUT     := build/web/aoe2.js
META    := build/web/aoe2.json
EXE     ?= orig/age2_x1.exe
.DEFAULT_GOAL := all
all: $(OUT) $(META)
PC_COPY := build/native/pcrecomp
PC      := $(PC_COPY)/runtime
PC_SRC  := third_party/pcrecomp
CC      := emcc

$(PC_COPY)/.patched:
	$(MAKE) -f tools/native/native.mk $@

GEN_SRC := $(sort $(wildcard $(GEN)/recomp_*.c)) $(GEN)/recomp_dispatch.c
HLE_SRC := $(addprefix $(PC)/win32hle/,$(notdir $(filter-out %_selftest.c,$(wildcard $(PC_SRC)/runtime/win32hle/*.c))))
$(PC)/win32hle/%.c: $(PC_COPY)/.patched ;
.SECONDARY:
APP_SRC := src/linux/main.c src/linux/runtime_glue.c src/linux/aoc_shims.c src/linux/win32_gaps.c src/linux/dplay_stub.c src/linux/cdrom.c \
           src/linux/aoc_inflate.c src/linux/ddraw_debug.c src/linux/edit_overlay.c src/linux/mciwnd.c src/linux/widescreen.c src/web/web_fs.c \
           src/net/dplay_net.c src/net/net_queue.c src/web/net_web.c

PORTS   := -sUSE_SDL=2 -sUSE_SDL_TTF=2 -sUSE_ZLIB=1
COMMON  := -g0 -fno-strict-aliasing -mtail-call -pthread -DRECOMP_FLAT_MEMORY $(PORTS) \
           -I$(PC)/recomp32 -I$(PC)/win32hle -I$(GEN) -Ithird_party/pl_mpeg
GEN_OPT ?= -O1
CFLAGS_GEN := $(GEN_OPT) -w -DRECOMP_LOCAL_REGS -include src/linux/widescreen_hooks.h $(COMMON)
CFLAGS_RT  := -O2 -w $(COMMON)

# The fonts win32hle's GDI and the edit overlay look for (Liberation, SIL OFL 1.1)
FONTS   := /usr/share/fonts/truetype/liberation
LDFLAGS := -O2 -mtail-call -pthread -sPROXY_TO_PTHREAD -sWASMFS $(PORTS) \
           -sGLOBAL_BASE=16777216 -sINITIAL_MEMORY=1024MB -sALLOW_MEMORY_GROWTH -sMAXIMUM_MEMORY=2048MB \
           -sSTACK_SIZE=16MB -sDEFAULT_PTHREAD_STACK_SIZE=8MB -sPTHREAD_POOL_SIZE=8 \
           -sEXIT_RUNTIME -sMODULARIZE -sEXPORT_NAME=AoE2 -sENVIRONMENT=web,worker \
           -sEXPORTED_RUNTIME_METHODS=HEAPU8,HEAPU16,HEAP32,HEAPU32,UTF8ToString,ENV \
           -sEXPORTED_FUNCTIONS=_main,_malloc,_free \
           --embed-file $(FONTS)@$(FONTS)

GEN_OBJ := $(patsubst $(GEN)/%.c,$(OBJ)/gen/%.o,$(GEN_SRC))
HLE_OBJ := $(patsubst $(PC)/win32hle/%.c,$(OBJ)/hle/%.o,$(HLE_SRC))
APP_OBJ := $(patsubst src/%.c,$(OBJ)/app/%.o,$(APP_SRC))

$(OUT): $(GEN_OBJ) $(HLE_OBJ) $(APP_OBJ)
	$(CC) -o $@ $^ $(LDFLAGS)

# Which age2_x1.exe this code was lifted from: the page checks a player's own copy
# against it before copying their install (another version would not run).
$(META): $(EXE)
	@mkdir -p $(dir $@)
	printf '{"exe": {"sha256": "%s", "size": %s}}\n' "$$(sha256sum $< | cut -d' ' -f1)" "$$(stat -c %s $<)" > $@

$(OBJ)/gen/%.o: $(GEN)/%.c | $(PC_COPY)/.patched
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS_GEN) -c $< -o $@

$(OBJ)/gen/recomp_0002.o $(OBJ)/gen/recomp_0004.o $(OBJ)/gen/recomp_0010.o: src/linux/widescreen_hooks.h $(PC_COPY)/.patched

$(OBJ)/hle/%.o: $(PC)/win32hle/%.c $(PC_COPY)/.patched
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS_RT) -c $< -o $@

$(OBJ)/app/%.o: src/%.c $(PC_COPY)/.patched
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS_RT) -Wall -c $< -o $@
