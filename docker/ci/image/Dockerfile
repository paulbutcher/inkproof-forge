# The image every CI job runs in. Toolchains live in the inkproof-elan volume,
# mounted at /opt/elan/toolchains, so each is downloaded once.
FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates curl git jq xz-utils zstd \
 && rm -rf /var/lib/apt/lists/*
ENV ELAN_HOME=/opt/elan
ENV PATH=/opt/elan/bin:$PATH
RUN curl -sSfL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh \
    | sh -s -- -y --no-modify-path --default-toolchain none \
 && mkdir -p /opt/elan/toolchains
