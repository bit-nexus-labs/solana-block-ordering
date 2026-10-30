# Probe binary
FROM rust:1.97-bookworm AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release --locked && strip target/release/ordprobe

FROM debian:bookworm-slim AS probe
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/target/release/ordprobe /usr/local/bin/ordprobe
WORKDIR /work
ENV OUT_DIR=/data KEYPAIR_PATH=/keys/probe.json
ENTRYPOINT ["ordprobe"]
CMD ["run"]

# Analysis
FROM python:3.12-slim AS analyze
WORKDIR /work
COPY analysis/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY analysis ./analysis
ENTRYPOINT ["python", "analysis/analyze.py"]
CMD ["--data", "/data", "--out", "/results"]
