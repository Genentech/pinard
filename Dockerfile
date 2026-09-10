# pinard — one image backing the website, the webterm-gateway, AND the memory
# services, selected by `command`:
#   webterm-gateway serve-site --dir /srv/site --addr :80   → static docs site (port 80)
#   webterm-gateway                                          → k8s web-terminal gateway (port 8080)
#   memory-ingester                                          → memory ingester
#   memory-recall                                            → memory recall service
#   memory-rollup                                            → scope roll-up engine
#   memory-curator                                           → per-vigne wiki curator
#
# Built by gpapy-asg-ci `.build-image` (docker build -f Dockerfile .). Multi-stage:
# render the Hugo docs, build all Go binaries, assemble a minimal runtime.
# No Python runtime required — all memory services are now native Go binaries.

ARG DEBIAN=debian:bookworm-slim
ARG GOLANG=golang:1.24-bookworm

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

# ── Stage 2: build all Go binaries ───────────────
FROM ${GOLANG} AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
        go build -trimpath -ldflags="-s -w" -o /out/webterm-gateway ./cmd/webterm-gateway && \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
        go build -trimpath -ldflags="-s -w" -o /out/memory-ingester ./cmd/memory-ingester && \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
        go build -trimpath -ldflags="-s -w" -o /out/memory-recall    ./cmd/memory-recall && \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
        go build -trimpath -ldflags="-s -w" -o /out/memory-rollup    ./cmd/memory-rollup && \
        go build -trimpath -ldflags="-s -w" -o /out/memory-curator   ./cmd/memory-curator

# ── Stage 3: minimal runtime ──────────────────────
FROM ${DEBIAN}
# ROCHE_CA_DEB_URL: when set, download and install the corporate internal CA .deb
# so the pod can reach intranet TLS (Rosetta, cloud Engram). Set in internal CI
# builds only; public/OSS builds leave this empty and rely on the Helm-mounted
# CA bundle (memory.caBundle) instead. No intranet URL appears in this file.
ARG ROCHE_CA_DEB_URL=""
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates git openssh-client wget \
    && if [ -n "$ROCHE_CA_DEB_URL" ]; then \
         wget -q "$ROCHE_CA_DEB_URL" -O roche-ca-certificates.deb \
         && dpkg -i roche-ca-certificates.deb \
         && rm -f roche-ca-certificates.deb \
         && update-ca-certificates; \
    fi \
    && apt-get purge -y wget && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/webterm-gateway   /usr/local/bin/webterm-gateway
COPY --from=build /out/memory-ingester   /usr/local/bin/memory-ingester
COPY --from=build /out/memory-recall     /usr/local/bin/memory-recall
COPY --from=build /out/memory-rollup     /usr/local/bin/memory-rollup
COPY --from=build /out/memory-curator    /usr/local/bin/memory-curator
COPY --from=site  /out/site              /srv/site
# Entrypoint script: if EXTRA_CA_CERTS points to a mounted CA bundle, append it
# to the system trust store before starting the main process.
RUN printf '#!/bin/sh\nset -e\nif [ -n "$EXTRA_CA_CERTS" ] && [ -f "$EXTRA_CA_CERTS" ]; then\n    cat "$EXTRA_CA_CERTS" >> /etc/ssl/certs/ca-certificates.crt\nfi\nexec "$@"\n' > /usr/local/bin/docker-entrypoint.sh \
    && chmod +x /usr/local/bin/docker-entrypoint.sh
WORKDIR /app
EXPOSE 80 8080
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh", "webterm-gateway"]
CMD []
