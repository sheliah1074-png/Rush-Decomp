# ---------------------------------------------------------------------------
# Rush-Decomp
#
# Every target that touches cartridge data reads it from baserom/ and writes it
# into data/, asm/, fsdump/ or build/.  baserom/, data/, fsdump/ and build/ are
# gitignored: they hold the image itself and verbatim chunks of it.  asm/ is
# TRACKED -- the disassembly is this project's work product and is committed.
# Nothing here downloads a ROM, and no target can put one in git.
#
# Start with:  make help
# ---------------------------------------------------------------------------

SHELL := /bin/bash

# Python interpreter.  Prefer the repo venv when it exists so that `make deps`
# and every other target agree on which environment is in play.
VENV      := .venv
VENV_PY   := $(VENV)/bin/python
PYTHON    := $(if $(wildcard $(VENV_PY)),$(VENV_PY),python3)

SR        := $(PYTHON) tools/sr
TESTS     := $(PYTHON) tests/test_pipeline.py
SELFTEST  := build/selftest.nds

ROM       ?= baserom/sonic_rush.nds

COMPONENTS := arm9 arm7
OVERLAYS   := $(wildcard data/*overlay*.bin)
OVERLAY_NAMES := $(patsubst data/%.bin,%,$(OVERLAYS))

.DEFAULT_GOAL := help
.PHONY: help deps venv setup selftest test lint clean clean-all \
        report extract dumpfs disasm disasm-all verify verify-all \
        rebuild progress assets check selfcheck

# --------------------------------------------------------------------------
help:
	@echo "Rush-Decomp"
	@echo
	@echo "This repository contains no cartridge image and no verbatim copy of one."
	@echo "Derived analysis (asm/, assets/) is committed on purpose."
	@echo "Dump your own cartridge into $(ROM) first -- see docs/ROM_SETUP.md."
	@echo
	@echo "Setup"
	@echo "  make deps          create $(VENV) and install capstone + keystone"
	@echo "  make selftest      build the synthetic test image and verify the"
	@echo "                     whole pipeline against it (no ROM needed)"
	@echo "  make test          run the test suite"
	@echo
	@echo "Working on the real image (needs $(ROM))"
	@echo "  make report        what is this cartridge?"
	@echo "  make extract       split it into data/"
	@echo "  make dumpfs        unpack the Nitro file system into fsdump/"
	@echo "  make disasm-all    disassemble every component into asm/"
	@echo "  make verify-all    disasm -> assemble -> compare, per component"
	@echo "  make rebuild       rebuild a full image and compare to the base ROM"
	@echo "  make progress      how much is expressed as C"
	@echo "  make assets        write the TRACKED artifacts under assets/"
	@echo "  make check         selftest + test + verify-all + rebuild"
	@echo
	@echo "Overrides"
	@echo "  make extract ROM=path/to/your.nds"

# --------------------------------------------------------------------------
venv:
	@test -x $(VENV_PY) || $(PYTHON) -m venv $(VENV)

deps: venv
	$(VENV_PY) -m pip install --upgrade pip >/dev/null
	$(VENV_PY) -m pip install -r requirements.txt
	@echo
	@echo "dependencies installed into $(VENV)/"

# --------------------------------------------------------------------------
# The synthetic image.  Built from code in this repository alone, so the
# pipeline is verifiable on a fresh clone and in CI.
selftest: $(SELFTEST)

$(SELFTEST): tests/make_test_rom.py
	@mkdir -p build
	$(PYTHON) tests/make_test_rom.py $(SELFTEST)

# Full pipeline against the synthetic image: extract, disassemble every
# component, verify each one byte for byte, then rebuild and compare.
.PHONY: selfcheck
selfcheck: $(SELFTEST)
	@set -e
	@echo "== extract =="
	$(SR) extract --rom $(SELFTEST) --banner --secure
	@echo
	@echo "== disasm + verify =="
	for c in arm9 arm7 $$(ls data | sed -n 's/^\(.*overlay.*\)\.bin$$/\1/p'); do \
		$(SR) disasm $$c >/dev/null; \
		$(SR) verify $$c --strict | head -2; \
	done
	@echo
	@echo "== rebuild =="
	$(SR) rebuild --rom $(SELFTEST) --keep-going
	@echo
	@cmp build/rebuilt.nds $(SELFTEST)
	@echo "selfcheck: rebuilt image is byte-identical to the synthetic base ROM"
	@echo
	@echo "== assets =="
	$(SR) assets --rom $(SELFTEST) --out-dir build/assets-selftest
	@test -s build/assets-selftest/layout.md
	@test -s build/assets-selftest/filesystem.txt
	@test -s build/assets-selftest/symbols/arm9.txt
	@echo "selfcheck: artifact generation wrote layout, listing and symbol maps"

test:
	$(TESTS)

lint:
	@$(PYTHON) -m compileall -q tools tests >/dev/null && echo "compileall: ok"
	@-$(PYTHON) -m pyflakes tools tests 2>/dev/null || \
		echo "(pyflakes not installed; skipped)"

# --------------------------------------------------------------------------
# Targets that operate on the real cartridge image.
$(ROM):
	@echo "error: no cartridge image at $(ROM)"
	@echo
	@echo "This project never contains a ROM, and none can be fetched for you."
	@echo "Dump your own copy of the cartridge and place it at:"
	@echo "    $(ROM)"
	@echo
	@echo "docs/ROM_SETUP.md explains how to dump, and how to confirm you have"
	@echo "the revision this project targets."
	@exit 1

report: $(ROM)
	$(SR) report --rom $(ROM)

extract: $(ROM)
	$(SR) extract --rom $(ROM) --banner

dumpfs: $(ROM)
	$(SR) dumpfs --rom $(ROM) --list

disasm: extract
	@for c in $(COMPONENTS); do $(SR) disasm $$c; done

disasm-all: extract
	@set -e
	@for c in $(COMPONENTS) $(OVERLAY_NAMES); do $(SR) disasm $$c; done

verify: disasm
	@for c in $(COMPONENTS); do $(SR) verify $$c --strict; done

verify-all: disasm-all
	@set -e
	@for c in $(COMPONENTS) $(OVERLAY_NAMES); do $(SR) verify $$c --strict; done

rebuild: verify-all
	$(SR) rebuild --rom $(ROM) --keep-going

progress:
	$(SR) progress --json build/progress.json

# The one verb that writes files meant to be COMMITTED.  Depends on disasm-all
# so the symbol maps have sidecars to draw from; without them it still writes
# layout.md and filesystem.txt and says why the maps are missing.
assets: disasm-all
	@echo "== assets =="
	$(SR) assets --rom $(ROM)

# Everything that can be checked without owning a cartridge.
check: selftest test selfcheck

# --------------------------------------------------------------------------
setup: deps selftest check
	@echo
	@echo "Setup complete.  The toolchain is verified against the synthetic"
	@echo "image.  Next: put your own dump at $(ROM) and run 'make report'."

# clean is deliberately asymmetric. data/, build/ and fsdump/ hold only ignored
# generated output, so git clean -x sweeps them while sparing their tracked
# .gitkeep placeholders. asm/ and assets/ hold COMMITTED work, so a blanket
# `git clean -fd` there deletes untracked files a contributor has not committed
# yet -- it destroyed both directories' READMEs on the first run after this
# policy changed. Only the known generated patterns are removed from asm/, and
# only when git does not already track them. assets/ is left entirely alone.
clean:
	@if [ -d .git ]; then \
		git clean -qfdx data build fsdump; \
		for f in asm/*.s asm/*.json; do \
			[ -e "$$f" ] || continue; \
			git ls-files --error-unmatch "$$f" >/dev/null 2>&1 || rm -f "$$f"; \
		done; \
	else \
		rm -rf build data/*.bin data/*.json data/*.ppm fsdump/*; \
		rm -f asm/*.s asm/*.json; \
	fi

clean-all: clean
	rm -rf $(VENV) tools/__pycache__ tools/rushdecomp/__pycache__ \
	       tests/__pycache__ .pytest_cache
