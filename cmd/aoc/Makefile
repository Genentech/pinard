SHELL := /bin/bash
BINARY := aoc
BUILD_DIR := ../../bin
# vendor/ contains only non-Go submodules (babysitter), so use module proxy.
export GOFLAGS ?= -mod=mod

.PHONY: build install test clean

build:
	CGO_ENABLED=0 go build -tags capsule -o $(BUILD_DIR)/$(BINARY)-go .

install:
	CGO_ENABLED=0 go build -tags capsule -o ~/.local/bin/$(BINARY) .

test:
	go test ./../../internal/...

clean:
	rm -f $(BUILD_DIR)/$(BINARY)-go
