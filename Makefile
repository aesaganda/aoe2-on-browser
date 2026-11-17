# Run inside Linux / WSL (Ubuntu 24.04):  make setup && make docs && make test
# From Windows:                            wsl -d Ubuntu-24.04 make docs
SHELL      := /bin/bash
EXE        ?= orig/age2_x1.exe
NAME       := $(basename $(notdir $(EXE)))
TOOLS_HOME ?= $(HOME)/.local/share/aoe2-recomp
PY         ?= $(TOOLS_HOME)/venv/bin/python
BUILD      := build
GHIDRA_JSON := $(BUILD)/ghidra/$(NAME).json

.PHONY: help setup recon ghidra docs test test-synthetic test-binary clean

help:
	@echo "make setup        one-time toolchain (venv, Ghidra) - see tools/setup-wsl.sh"
	@echo "make ghidra       headless Ghidra analysis -> docs/ghidra.md"
	@echo "make recon        PE/import/COM recon      -> docs/recon.md, docs/imports.md"
	@echo "make docs         both of the above"
	@echo "make test         differential tests (lifted C vs Unicorn running the original bytes)"
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

clean:
	rm -rf $(BUILD)
