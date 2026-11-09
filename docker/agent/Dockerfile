# The image every agent container runs: Claude Code, tea, git, Node (for
# Lasm), Python (for the runner and lean-lsp-mcp), and the kit's policy and
# helpers, which the agent can read but not change. Each agent gets its own
# small final stage, which bakes in its managed CLAUDE.md (role, then
# protocol) and the git rules that send this project's GitHub URLs to Forgejo.
#
# Versions are pinned: every agent runs the same tools during an experiment.
FROM node:24.18.1-trixie-slim AS base

ARG CLAUDE_CODE_VERSION=2.1.311
ARG TEA_VERSION=0.16.0
ARG TEA_SHA256_AMD64=9c142c8a0b31485eea2387031c1c404f12b7141075eb4ffbd7ced49441590636
ARG TEA_SHA256_ARM64=0e14a036322eeb1fc55e266acafeb8490e523ed99f9232034ae74861e2339879
ARG LEAN_LSP_MCP_VERSION=0.32.0
ARG USER_UID=1000
ARG USER_GID=1000
ARG TARGETARCH

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq less procps python3 python3-pip xz-utils util-linux \
 && rm -rf /var/lib/apt/lists/*

# lean-lsp-mcp, the Lean tools the repositories' CLAUDE.md asks agents to use.
RUN pip install --no-cache-dir --break-system-packages "lean-lsp-mcp==${LEAN_LSP_MCP_VERSION}"

# tea, Forgejo's command line, checked against its published checksum.
RUN case "$TARGETARCH" in \
      amd64) sum="$TEA_SHA256_AMD64" ;; \
      arm64) sum="$TEA_SHA256_ARM64" ;; \
      *) echo "no tea build for $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && curl -fsSL -o /usr/local/bin/tea \
      "https://gitea.com/gitea/tea/releases/download/v${TEA_VERSION}/tea-${TEA_VERSION}-linux-${TARGETARCH}" \
 && echo "$sum  /usr/local/bin/tea" | sha256sum -c - \
 && chmod 0755 /usr/local/bin/tea

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" && npm cache clean --force

# The agent's user, with the host user's IDs so it can read its secrets.
# -o allows IDs the base image already uses (macOS's GID 20, node's 1000).
RUN userdel -r node \
 && groupadd -o -g "${USER_GID}" agent \
 && useradd -o -m -u "${USER_UID}" -g "${USER_GID}" -s /bin/bash agent \
 && mkdir -p /home/agent/.claude /home/agent/.elan /home/agent/.config/tea \
             /workspace /var/lib/inkproof/archive /etc/claude-code /usr/local/share/inkproof \
 && chown -R agent:agent /home/agent /workspace /var/lib/inkproof

COPY --chmod=0755 bin/ /usr/local/bin/
COPY --chmod=0644 managed-settings.json /etc/claude-code/managed-settings.json
COPY --chmod=0644 PROTOCOL.md /usr/local/share/inkproof/PROTOCOL.md
COPY --from=roles --chmod=0644 . /usr/local/share/inkproof/roles/

ENV PATH=/home/agent/.elan/bin:/usr/local/bin:/usr/bin:/bin \
    CLAUDE_CONFIG_DIR=/home/agent/.claude \
    DISABLE_AUTOUPDATER=1

FROM base AS agent
ARG AGENT
ARG AGENT_REPO
ARG FORGE_ORG
ARG HUMAN_USER
# Space-separated GitHub owners whose inkproof- URLs mean this project.
ARG GH_OWNERS
RUN test -n "$AGENT" && test -n "$AGENT_REPO" && test -n "$FORGE_ORG" && test -n "$HUMAN_USER" && test -n "$GH_OWNERS" \
 && /usr/local/bin/inkproof-render "$AGENT" "$AGENT_REPO" "$FORGE_ORG" "$HUMAN_USER" "$GH_OWNERS"

USER agent
WORKDIR /workspace
CMD ["/usr/local/bin/agent-init"]
