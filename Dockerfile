# pinard — one image backing the website, the webterm-gateway, AND the memory
# service, selected by `command`:
#   webterm-gateway serve-site --dir /srv/site --addr :80   → static docs site (port 80)
#   webterm-gateway                                          → k8s web-terminal gateway (port 8080)
#   python -m services.memory.ingester                       → memory ingester (+ recall/rollup/promotion)
#
# Built by gpapy-asg-ci `.build-image` (docker build -f Dockerfile .). Multi-stage:
# render the Hugo docs, build the Go gateway binary, install Python deps, assemble
# a runtime with all three. No nginx, no separate images.

ARG DEBIAN=debian:bookworm-slim
ARG GOLANG=golang:1.24-bookworm
ARG PYTHON=python:3.11-slim-bookworm

# ── Stage 1: render the Hugo site ────────────────
FROM ${DEBIAN} AS site
ARG HUGO_VERSION=0.148.1
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates wget git gcc g++ libc-dev && \
    wget -q https://go.dev/dl/go1.24.7.linux-amd64.tar.gz && \
        tar xzf go1.24.7.linux-amd64.tar.gz -C /usr/local && rm go1.24.7.linux-amd64.tar.gz && \
    wget -q https://github.com/gohugoio/hugo/releases/download/v${HUGO_VERSION}/hugo_extended_${HUGO_VERSION}_linux-amd64.tar.gz && \
        tar xzf hugo_extended_${HUGO_VERSION}_linux-amd64.tar.gz -C /usr/local/bin && \
        rm hugo_extended_${HUGO_VERSION}_linux-amd64.tar.gz && \
    rm -rf /var/lib/apt/lists/*
ENV PATH="/usr/local/go/bin:${PATH}"
WORKDIR /src
COPY website/ ./website/
RUN cd website && hugo --minify --destination /out/site

# ── Stage 2: build the Go gateway binary ─────────
FROM ${GOLANG} AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
        go build -trimpath -ldflags="-s -w" -o /out/webterm-gateway ./cmd/webterm-gateway

# ── Stage 3: install Python dependencies ─────────
FROM ${PYTHON} AS pydeps
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY packages/pinard-core/ ./packages/pinard-core/
RUN pip install --no-cache-dir ./packages/pinard-core
COPY services/memory/requirements.txt ./services/memory/requirements.txt
RUN pip install --no-cache-dir -r services/memory/requirements.txt

# ── Stage 4: runtime ─────────────────────────────
FROM ${PYTHON}
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git openssh-client \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build   /out/webterm-gateway        /usr/local/bin/webterm-gateway
COPY --from=site    /out/site                   /srv/site
COPY --from=pydeps  /usr/local/lib/python3.11/site-packages \
                    /usr/local/lib/python3.11/site-packages
COPY services/ /app/services/
COPY packages/pinard-core/ /app/packages/pinard-core/
WORKDIR /app
EXPOSE 80 8080
ENTRYPOINT ["webterm-gateway"]
