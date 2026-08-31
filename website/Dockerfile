# Stage 1: Build the Hugo site
FROM debian:bookworm-slim AS builder

ARG HUGO_VERSION=0.148.1

# Install Hugo Extended + Go (for Hugo modules)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates wget git gcc g++ libc-dev && \
    wget -q https://go.dev/dl/go1.24.7.linux-amd64.tar.gz && \
    tar xzf go1.24.7.linux-amd64.tar.gz -C /usr/local && \
    rm go1.24.7.linux-amd64.tar.gz && \
    wget -q https://github.com/gohugoio/hugo/releases/download/v${HUGO_VERSION}/hugo_extended_${HUGO_VERSION}_linux-amd64.tar.gz && \
    tar xzf hugo_extended_${HUGO_VERSION}_linux-amd64.tar.gz -C /usr/local/bin && \
    rm hugo_extended_${HUGO_VERSION}_linux-amd64.tar.gz && \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/usr/local/go/bin:${PATH}"

WORKDIR /app
COPY . .

RUN hugo --minify

# Stage 2: Serve with nginx
FROM nginx:latest

ENV NGINX_WORKERS=1

COPY --from=builder /app/public /usr/share/nginx/html

RUN sed -i "s/worker_processes.*auto;/worker_processes ${NGINX_WORKERS};/" /etc/nginx/nginx.conf

EXPOSE 80
