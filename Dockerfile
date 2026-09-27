FROM rust:1.97-slim-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends cmake make pkg-config libssl-dev libvips-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY static ./static
RUN cargo build --release

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libssl3 libvips-tools ffmpeg poppler-utils gosu && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home-dir /app rustfire \
    && mkdir -p /app /data/uploads \
    && chown -R rustfire:rustfire /app /data
WORKDIR /app
COPY --from=build /build/target/release/rustfire /usr/local/bin/rustfire
COPY static ./static
COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/rustfire-entrypoint
ENV RUSTFIRE_ADDR=0.0.0.0:3000 \
    RUSTFIRE_DB=/data/rustfire.db \
    RUSTFIRE_UPLOAD_DIR=/data/uploads
EXPOSE 3000
ENTRYPOINT ["/usr/local/bin/rustfire-entrypoint"]
CMD ["rustfire"]
