# Native Linux build (Phase 1): lifted code + pcrecomp recomp32/win32hle + SDL2.
# Invoked by the top-level `make native` after `make lift` (the gen file list
# only exists once the lift has run, hence a separate makefile).
GEN     := build/lift/gen
PC      := third_party/pcrecomp/runtime
OBJ     := build/native/obj
BIN     := build/native/aoe2

GEN_SRC := $(sort $(wildcard $(GEN)/recomp_*.c)) $(GEN)/recomp_dispatch.c
HLE_SRC := $(filter-out %_selftest.c,$(wildcard $(PC)/win32hle/*.c))
# recomp32's trace and crash reporter include <windows.h> (they serve native32), so the
# few symbols the lifted code expects from them come from src/linux/runtime_glue.c.
RT_SRC  :=
APP_SRC := src/linux/main.c src/linux/runtime_glue.c src/linux/aoc_shims.c

SDL_CFLAGS := $(shell pkg-config --cflags sdl2 SDL2_ttf)
SDL_LIBS   := $(shell pkg-config --libs sdl2 SDL2_ttf)

# The lift is 50 MB of C: -O1 keeps the build to minutes; hot paths get -O2 later.
GEN_OPT  ?= -O1
CFLAGS_COMMON := -g -fno-strict-aliasing -I$(PC)/recomp32 -I$(PC)/win32hle -I$(GEN)
CFLAGS_GEN := $(GEN_OPT) -w -DRECOMP_LOCAL_REGS $(CFLAGS_COMMON)
CFLAGS_RT  := -O1 -w $(CFLAGS_COMMON) $(SDL_CFLAGS)

GEN_OBJ := $(patsubst $(GEN)/%.c,$(OBJ)/gen/%.o,$(GEN_SRC))
HLE_OBJ := $(patsubst $(PC)/win32hle/%.c,$(OBJ)/hle/%.o,$(HLE_SRC))
RT_OBJ  := $(patsubst $(PC)/recomp32/%.c,$(OBJ)/rt/%.o,$(RT_SRC))
APP_OBJ := $(patsubst src/linux/%.c,$(OBJ)/app/%.o,$(APP_SRC))

$(BIN): $(GEN_OBJ) $(HLE_OBJ) $(RT_OBJ) $(APP_OBJ)
	$(CC) -o $@ $^ $(SDL_LIBS) -lpthread -lm

$(OBJ)/gen/%.o: $(GEN)/%.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS_GEN) -c $< -o $@

$(OBJ)/hle/%.o: $(PC)/win32hle/%.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS_RT) -c $< -o $@

$(OBJ)/rt/%.o: $(PC)/recomp32/%.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS_RT) -c $< -o $@

$(OBJ)/app/%.o: src/linux/%.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS_RT) -Wall -c $< -o $@
