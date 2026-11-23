#!/usr/bin/env bash
# The kit's state, as scripts/status.sh ends with it, kept up to date: whether
# it is working, stuck or quiet, and what each agent is doing; then what each
# agent owes, one line each, as status.sh's Owed section. Ctrl-C ends.
#   scripts/watch.sh          redraw every 15 seconds
#   scripts/watch.sh -n 60    every 60 seconds
# Not to a terminal, it prints the state with the time whenever it changes.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
every=15
while [ $# -gt 0 ]; do
  case "$1" in
    -n) every="${2:?-n needs a number of seconds}"; shift 2 ;;
    -h|--help) sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "usage: scripts/watch.sh [-n SECONDS]" >&2; exit 2 ;;
  esac
done

last=""
while :; do
  summary="$(docker compose exec -T dispatcher python3 -c '
import json, sys, urllib.request
s = json.load(urllib.request.urlopen("http://127.0.0.1:8080/status", timeout=60))
out = [s.get("summary") or "Kit: unknown (rebuild the dispatcher: scripts/up.sh)"]
owed = s.get("obligations") or []
if owed:
    out.append("")
    out.append("Owed:")
    for o in owed:
        d = o["description"] if len(o["description"]) <= 110 else o["description"][:109].rstrip() + "\u2026"
        mine = ", now yours" if o["escalated"] else ""
        out.append("  - %s: %s (woken %sx%s)" % (o["agent"], d, o["wakes"], mine))
sys.stdout.write("\n".join(out))' 2>/dev/null)" \
    || summary="(the dispatcher isn't answering; is the kit up? scripts/up.sh)"
  if [ -t 1 ]; then
    printf '\033[H\033[2J%s  (every %ss; Ctrl-C ends)\n\n%s\n' "$(date +%H:%M:%S)" "$every" "$summary"
  elif [ "$summary" != "$last" ]; then
    printf '%s\n%s\n\n' "$(date +%H:%M:%S)" "$summary"
  fi
  last="$summary"
  sleep "$every"
done
