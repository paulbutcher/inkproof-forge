#!/usr/bin/env bash
# What the kit is doing, as it happens: Forgejo events (pushes, pull
# requests, reviews, labels, CI, releases), work queued for each agent, runs
# starting and ending, your sessions in agent containers, and notices.
#   scripts/activity.sh            the last 40 entries, then follow (Ctrl-C ends)
#   scripts/activity.sh scale      only lines mentioning scale
#   scripts/activity.sh -n 200     the last 200 entries first
#   scripts/activity.sh --once     the last entries, without following
#   scripts/activity.sh --urls     Forgejo addresses at the end of each line, for
#                                  terminals without hyperlinks
# In a terminal, each issue and pull request named (inkproof-core#24) links to
# Forgejo: Cmd-click it.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
limit=40 follow=1 filter="" links=osc
while [ $# -gt 0 ]; do
  case "$1" in
    -n) limit="${2:?-n needs a number}"; shift 2 ;;
    --once) follow=""; shift ;;
    --urls) links=urls; shift ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "usage: scripts/activity.sh [-n N] [--once] [--urls] [filter]" >&2; exit 2 ;;
    *) filter="$1"; shift ;;
  esac
done
# This machine's offset from UTC, for the times shown.
z="$(date +%z)"
tz=$(( (10#${z:1:2} * 3600 + 10#${z:3:2} * 60) * (${z:0:1}1) ))
# Links only to a terminal, unless asked for: escapes would clutter a file.
[ -t 1 ] || [ "$links" = urls ] || links=""
web="http://localhost:${FORGE_PORT:-3000}/$FORGE_ORG"

state="" warned=""
while :; do
  if out="$(docker compose exec -T -e ACTIVITY_LINKS="${links:+$web}" -e ACTIVITY_LINK_STYLE="$links" dispatcher activity "$state" "$tz" "$limit" "$filter" 2>/dev/null)"; then
    warned=""
    printf '%s\n' "$out" | sed '$d' | grep -v '^$' || true
    state="$(printf '%s\n' "$out" | tail -n 1 | cut -f 2)"
  elif [ -z "$follow" ] || [ -z "$warned" ]; then
    echo "(the dispatcher isn't answering; is the kit up? scripts/up.sh)" >&2
    [ -n "$follow" ] || exit 1
    warned=1
  fi
  [ -n "$follow" ] || exit 0
  sleep 5
done
