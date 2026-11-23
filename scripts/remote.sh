#!/usr/bin/env bash
# Your sessions with agents through Remote Control: each runs in tmux in the
# agent's container, named after the agent, and you drive it from the Claude
# desktop app, claude.ai/code or the mobile app, signed in to the agents'
# Claude account. It is one of your sessions like any in VS Code: it
# consults, and the same guard applies.
#   scripts/remote.sh [<agent>...]        start a session for each agent (every
#                                         running agent if none is named)
#   scripts/remote.sh --attach <agent>    attach to it in this terminal, starting
#                                         it if need be (Ctrl-b d to detach)
#   scripts/remote.sh --stop [<agent>...] end the sessions
# The first time for each agent, attach: it asks you to log in (and Claude
# may ask you to enable Remote Control). Sessions end when their containers
# are recreated (scripts/up.sh after a change): start them again.
set -euo pipefail
. "$(dirname "$0")/lib.sh"

mode=start
case "${1:-}" in
  --attach) mode=attach; shift; [ $# = 1 ] || { echo "usage: scripts/remote.sh --attach <agent>" >&2; exit 2; } ;;
  --stop) mode=stop; shift ;;
  -*) echo "usage: scripts/remote.sh [--attach <agent> | --stop] [<agent>...]" >&2; exit 2 ;;
esac

if [ $# = 0 ]; then
  set -- $(awk '!/^[[:space:]]*(#|$)/ { print $1 }' agents.conf)
  all=1
fi

running() { [ -n "$(docker compose ps -q "$1" 2>/dev/null)" ]; }
has_session() { docker compose exec -T -u agent "$1" tmux has-session -t human 2>/dev/null; }
logged_in() { docker compose exec -T -u agent "$1" env -u CLAUDE_CODE_OAUTH_TOKEN claude auth status >/dev/null 2>&1; }
start() { docker compose exec -T -u agent "$1" tmux new-session -d -s human -x 200 -y 50 agent-remote; }

status=0
for agent in "$@"; do
  awk -v a="$agent" '$1 == a { found = 1 } END { exit !found }' agents.conf \
    || { echo "$agent isn't in agents.conf" >&2; status=1; continue; }
  if ! running "$agent"; then
    [ -n "${all:-}" ] || { echo "$agent isn't running; start it with scripts/up.sh" >&2; status=1; }
    continue
  fi
  case "$mode" in
    attach)
      exec docker compose exec -it -u agent "$agent" tmux new-session -A -s human -x 200 -y 50 agent-remote ;;
    stop)
      if has_session "$agent"; then
        docker compose exec -T -u agent "$agent" tmux kill-session -t human && echo "$agent: ended"
      fi ;;
    start)
      if has_session "$agent"; then
        echo "$agent: already running"
      elif ! logged_in "$agent"; then
        echo "$agent: not logged in; run scripts/remote.sh --attach $agent once to log in"
        status=1
      else
        start "$agent"
        sleep 8
        screen="$(docker compose exec -T -u agent "$agent" tmux capture-pane -p -t human 2>/dev/null || true)"
        if grep -qiE 'enable remote control|\(y/n\)|trust' <<<"$screen"; then
          echo "$agent: started, but Claude is asking something; answer it with scripts/remote.sh --attach $agent"
        elif ! has_session "$agent"; then
          echo "$agent: Claude exited at once; see why with scripts/remote.sh --attach $agent" >&2
          status=1
        else
          echo "$agent: started; it shows as \"$agent\" in the Claude app"
        fi
      fi ;;
  esac
done
exit "$status"
