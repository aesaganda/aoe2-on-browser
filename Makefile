# Run inside Linux / WSL (Ubuntu 24.04):  make setup && make docs && make test
# From Windows:                            wsl -d Ubuntu-24.04 make docs
SHELL      := /bin/bash
.SHELLFLAGS := -o pipefail -c
EXE        ?= orig/age2_x1.exe
NAME       := $(basename $(notdir $(EXE)))
TOOLS_HOME ?= $(HOME)/.local/share/aoe2-recomp
PY         ?= $(TOOLS_HOME)/venv/bin/python
BUILD      := build
GHIDRA_JSON := $(BUILD)/ghidra/$(NAME).json

GEN        := $(BUILD)/lift/gen
GEN_STAMP  := $(GEN)/recomp_funcs.h
PYTEST     := $(PY) -m pytest -q -p no:cacheprovider -n auto -rA

.PHONY: help setup recon ghidra docs lift native test test-synthetic test-binary test-pcrecomp sweep clean

help:
	@echo "make setup        one-time toolchain (venv, Ghidra) - see tools/setup-wsl.sh"
	@echo "make ghidra       headless Ghidra analysis -> docs/ghidra.md"
	@echo "make recon        PE/import/COM recon      -> docs/recon.md, docs/imports.md"
	@echo "make docs         both of the above"
	@echo "make lift         whole-image lift with pcrecomp -> build/lift/gen (needs EXE)"
	@echo "make test         all tests: pcrecomp self-test, synthetic PE, and the AoE2 prototype if EXE exists"
	@echo "EXE=$(EXE)"

setup:
	bash tools/setup-wsl.sh

$(GHIDRA_JSON): $(EXE) tools/ghidra/ExportAnalysis.java tools/ghidra/run.sh
	bash tools/ghidra/run.sh $(EXE) $@

ghidra: $(GHIDRA_JSON)
	$(PY) tools/ghidra/report.py $(GHIDRA_JSON) --out docs/ghidra.md --leaves $(BUILD)/ghidra/leaves.json

recon: $(GHIDRA_JSON)
	$(PY) tools/recon/recon.py $(EXE) --ghidra $(GHIDRA_JSON) --out docs

docs: ghidra recon

$(GEN_STAMP): $(EXE) $(GHIDRA_JSON) tools/lift/lift_image.py
	$(PY) tools/lift/lift_image.py $(EXE) $(GHIDRA_JSON) $(GEN) --stats $(BUILD)/lift/stats.json

lift: $(GEN_STAMP)

# pcrecomp's own instruction-level suite (380 cases vs Unicorn). glibc needs -lm on its link line.
test-pcrecomp:
	cd third_party/pcrecomp && RECOMP_CC="$(CURDIR)/tools/cc-lm.sh" $(PY) tools/lift/difftest.py | tail -1

test-synthetic:
	$(PYTEST) tests/test_synthetic.py

# The AoE2 prototype needs the user's binary (ADR-0002); without it the tests skip.
test-binary: $(if $(wildcard $(EXE)),$(GEN_STAMP))
	$(PYTEST) tests/test_aoe2_prototype.py

test: test-pcrecomp test-synthetic test-binary

# Phase 1: native Linux binary (lifted code + pcrecomp win32hle + SDL2) -> build/native/aoe2
native: $(GEN_STAMP)
	$(MAKE) -f tools/native/native.mk -j$$(nproc)

# Every leaf function (not just the 20 prototypes), 150 random states each: ~13 min on 20 cores.
sweep: $(GEN_STAMP)
	$(PY) tools/lift/sweep.py $(EXE) $(GEN) $(BUILD)/ghidra/leaves.json --out $(BUILD)/lift/sweep.json | tee $(BUILD)/lift/sweep.log

clean:
	rm -rf $(BUILD)
