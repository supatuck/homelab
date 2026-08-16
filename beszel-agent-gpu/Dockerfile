# Beszel agent with a working GPU collector for Intel Arc Battlemage.
#
# The official henrygd/beszel-agent-intel image ships only intel_gpu_top, which
# is i915-only and fails on this card with "no discrete/integrated i915 devices
# found" (verified by running it inside that very image). Beszel's docs list
# nvtop as an alternative multi-vendor collector, but require 3.3.2+, and every
# distro package is stuck at 3.2.0 — so build it.
#
# The upstream agent image is distroless, so its binary is copied onto a Debian
# base rather than the other way round.

FROM debian:trixie-slim AS nvtop

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

FROM debian:trixie-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 libdrm2 libudev1 libncursesw6 libtinfo6 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=henrygd/beszel-agent:latest /agent /agent
COPY --from=nvtop /usr/local/bin/nvtop /usr/local/bin/nvtop

# nvtop reports this card fine but returns device_name: null, and Beszel keys
# GPUs by name -- its collector logs "GPU data=map[]" and shows nothing. The
# shim fills the name in. /usr/local/sbin precedes /usr/local/bin on PATH, so
# the agent resolves the shim while the real binary stays reachable.
COPY nvtop-shim.py /usr/local/sbin/nvtop
RUN chmod +x /usr/local/sbin/nvtop

ENTRYPOINT ["/agent"]
