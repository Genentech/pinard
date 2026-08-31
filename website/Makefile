SHELL := /bin/bash
GITBRANCH = $(shell git rev-parse --abbrev-ref HEAD)
GITHASH = $(shell git rev-parse --short HEAD)
GITTAG = $(shell git describe --tags --exact-match HEAD 2> /dev/null)
GITTAG_CLEAN = $(subst v,,$(GITTAG))
BUILD_TAG = $(if $(GITTAG),$(GITTAG_CLEAN),$(subst /,-,$(GITBRANCH))-$(GITHASH))

DOCKER_REPOSITORY ?= your-registry.example.com
DOCKER_LOCAL_REPOSITORY = localhost:5000

PROJECT_NAME = pinard-website
DEV_HOST ?= localhost
DEV_PORT ?= 8888
BUILD_IMAGE_NAME = gp/${PROJECT_NAME}

# ── Build ────────────────────────────────────────

image:
	docker build \
		-t ${BUILD_IMAGE_NAME}:${BUILD_TAG} \
		-f Dockerfile .

# ── Push ─────────────────────────────────────────

push:
	docker tag ${BUILD_IMAGE_NAME}:${BUILD_TAG} ${DOCKER_REPOSITORY}/${BUILD_IMAGE_NAME}:${BUILD_TAG}
	docker push ${DOCKER_REPOSITORY}/${BUILD_IMAGE_NAME}:${BUILD_TAG}

dev-push:
	docker tag ${BUILD_IMAGE_NAME}:${BUILD_TAG} ${DOCKER_LOCAL_REPOSITORY}/${BUILD_IMAGE_NAME}:${BUILD_TAG}
	docker tag ${BUILD_IMAGE_NAME}:${BUILD_TAG} ${DOCKER_LOCAL_REPOSITORY}/${BUILD_IMAGE_NAME}:latest
	docker push ${DOCKER_LOCAL_REPOSITORY}/${BUILD_IMAGE_NAME}:${BUILD_TAG}
	docker push ${DOCKER_LOCAL_REPOSITORY}/${BUILD_IMAGE_NAME}:latest

# ── Local dev ────────────────────────────────────

serve:
	hugo server --bind 0.0.0.0 --port ${DEV_PORT} --baseURL http://$(DEV_HOST):${DEV_PORT}/

build:
	hugo --minify

clean:
	rm -rf public/ resources/

# ── Shortcuts ────────────────────────────────────

# Build + push to registry
release: image push

# Build + push to local k3d registry
dev-release: image dev-push

.PHONY: image push dev-push serve build clean release dev-release
