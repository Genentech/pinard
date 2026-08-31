SHELL := /bin/bash

.PHONY: build install test typecheck dist clean-dist base os

# Build + install the aoc Go binary (delegates to cmd/aoc).
build:
	$(MAKE) -C cmd/aoc build

install:
	$(MAKE) -C cmd/aoc install

test:
	go test ./internal/... ./cmd/aoc/

# Type-check the Pi extensions (see pi-extension/tsconfig.json).
typecheck:
	cd pi-extension && npm run --silent typecheck

# Build the single-file Linux/glibc distribution: dist/pinard-linux-x64.run
# Bundles aoc + launcher + extensions + a vendored Node/Pi runtime.
dist:
	bash dist/build.sh

clean-dist:
	rm -rf dist/stage dist/pinard-linux-x64.run

# ── Singularity images (two-layer for faster rebuilds) ──────────────
# pinard-os.sif   : system deps + corporate CA (stable; rebuilt only when its def or
#                   build-sif.sh change — cached as a file target).
# pinard-base.sif : pinard-os.sif + the make-dist runtime (rebuilt each `make dist`,
#                   but skips the dnf/CA work).
# Privilege + scratch knobs (both forwarded to build-sif.sh):
#   BUILD_MODE=sudo|fakeroot|remote  — auto-detected when unset.
#   SINGULARITY_TMPDIR=<dir>         — roomy scratch for the build sandbox +
#                                      makeself extraction (carried through sudo).
# Override on the command line, e.g.:
#   make base BUILD_MODE=sudo SINGULARITY_TMPDIR=/data/storage/tmp/singularity
SINGULARITY_TMPDIR ?= /data/storage/tmp/singularity
OS_SIF := $(CURDIR)/dist/singularity/pinard-os.sif

os: $(OS_SIF)
$(OS_SIF): dist/singularity/pinard-os.def dist/singularity/build-sif.sh
	STAGE_RUN=0 DEF=$(CURDIR)/dist/singularity/pinard-os.def OUT=$(OS_SIF) \
	  BUILD_MODE=$(BUILD_MODE) SINGULARITY_TMPDIR=$(SINGULARITY_TMPDIR) bash dist/singularity/build-sif.sh

# `base` depends on `dist` (fresh .run → current bin/pinard + aoc) and the os layer
# (built only if missing/changed). The runtime extraction + engram bake rerun each
# build; the dnf/CA work in the os layer does not. This is the one-shot for a full
# base rebuild after a code change (steps: dist → base).
base: dist $(OS_SIF)
	BUILD_MODE=$(BUILD_MODE) SINGULARITY_TMPDIR=$(SINGULARITY_TMPDIR) bash dist/singularity/build-sif.sh
