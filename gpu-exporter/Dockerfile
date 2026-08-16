# nvtop is the only tool that reads Intel Arc Battlemage (xe driver) engine
# counters. Every distro package is stuck at 3.2.0, which predates Battlemage
# support and reports "No GPU to monitor" on this card (verified against
# debian trixie/sid and ubuntu 25.10/26.04), so build from source. 3.3.2 also
# fixes gpu_util and power_draw reporting null on this card, which 3.3.1 did.
# Pinned to a tag, not a branch, so rebuilds are reproducible.
FROM debian:trixie-slim AS build

ARG NVTOP_VERSION=3.3.2

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git cmake build-essential pkg-config \
      libdrm-dev libudev-dev libncurses-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${NVTOP_VERSION}" \
      https://github.com/Syllo/nvtop.git /src \
 && cmake -S /src -B /build \
      -DCMAKE_BUILD_TYPE=Release \
      -DNVIDIA_SUPPORT=OFF -DAMDGPU_SUPPORT=OFF -DINTEL_SUPPORT=ON \
 && cmake --build /build --parallel "$(nproc)" \
 && cmake --install /build

# ---------------------------------------------------------------------------
# Runtime: no toolchain, just nvtop plus the stdlib-only exporter.
FROM debian:trixie-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 libdrm2 libudev1 libncursesw6 libtinfo6 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local/bin/nvtop /usr/local/bin/nvtop
COPY exporter.py /usr/local/bin/exporter.py

EXPOSE 8000
CMD ["python3", "/usr/local/bin/exporter.py"]
