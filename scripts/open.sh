#!/usr/bin/env bash
# Open VS Code attached to an agent's running container, at its workspace,
# with its live run log (.inkproof/current.md) in a tab. Dev Containers
# builds nothing: the container is already running from scripts/up.sh.
#   scripts/open.sh core
set -euo pipefail
. "$(dirname "$0")/lib.sh"
[ $# = 1 ] || { echo "usage: scripts/open.sh <agent>" >&2; exit 2; }
agent="$1"
repo="$(awk -v a="$agent" '$1 == a { print $3 }' agents.conf)"
[ -n "$repo" ] && [ "$repo" != - ] || { echo "$agent has no repository in agents.conf" >&2; exit 1; }
container="$(docker compose ps -q "$agent")"
[ -n "$container" ] || { echo "$agent isn't running; start it with scripts/up.sh" >&2; exit 1; }
name="$(docker inspect -f '{{.Name}}' "$container" | sed 's#^/##')"
command -v code >/dev/null || { echo "Put VS Code's 'code' command on your PATH (Command Palette: Shell Command: Install 'code' command in PATH)." >&2; exit 1; }

# The attach configuration Dev Containers uses for this container: the agent's
# user, its workspace and the extensions. Turning off the host's git config
# and credentials is a VS Code user setting (see README).
case "$(uname)" in
  Darwin) cfg_dir="$HOME/Library/Application Support/Code/User/globalStorage/ms-vscode-remote.remote-containers/nameConfigs" ;;
  *) cfg_dir="$HOME/.config/Code/User/globalStorage/ms-vscode-remote.remote-containers/nameConfigs" ;;
esac
mkdir -p "$cfg_dir"
cat > "$cfg_dir/$name.json" <<JSON
{
  "remoteUser": "agent",
  "workspaceFolder": "/workspace/$repo",
  "extensions": ["anthropic.claude-code", "leanprover.lean4"],
  "settings": {
    "remote.autoForwardPorts": false
  }
}
JSON

hex="$(printf '{"containerName":"/%s"}' "$name" | od -An -tx1 | tr -d ' \n')"
code --folder-uri "vscode-remote://attached-container+$hex/workspace/$repo"
code --file-uri "vscode-remote://attached-container+$hex/workspace/$repo/.inkproof/current.md" 2>/dev/null || true
