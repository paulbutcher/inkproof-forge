# The image every CI job runs in. Toolchains live in the inkproof-elan volume,
# mounted at /opt/elan/toolchains, so each is downloaded once. Node is there
# for Lasm, which repositories pin in test/package-lock.json.
FROM node:24.18.1-trixie-slim AS node

FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates curl git jq xz-utils zstd \
 && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && node --version && npm --version
# Headless Chromium for browser tests, as in the agents' image and at the same
# Playwright version (docker/agent/Dockerfile): jobs can't download browsers,
# so repositories pin `playwright` at exactly this version.
ARG PLAYWRIGHT_VERSION=1.64.1
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN npx -y "playwright@${PLAYWRIGHT_VERSION}" install --with-deps --only-shell chromium \
 && chmod -R a+rX /opt/ms-playwright \
 && rm -rf /root/.npm /var/lib/apt/lists/*
ENV ELAN_HOME=/opt/elan
ENV PATH=/opt/elan/bin:$PATH
RUN curl -sSfL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh \
    | sh -s -- -y --no-modify-path --default-toolchain none \
 && mkdir -p /opt/elan/toolchains
